from telethon.sync import TelegramClient
from telethon import events
import re
import asyncio
import requests
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from datetime import datetime

# Настройки
API_ID = '33396066'
API_HASH = '7b07798ccf361c0dff0c57032dcf45c9'
PHONE = '+79046112706'
GROUP = 'PRO_TRADE_IN777'

# Бот для уведомлений
BOT_TOKEN = '8377053351:AAHtSvrUqNlMWWBvmGhtyiYl1eKk5FAJk6c'
YOUR_ID = '1124062109'

# Глобальные переменные для хранения состояния
monitored_urls = set()
last_messages = {}

def is_webapp_link(url):
    """Проверяет, является ли ссылка Web App"""
    return any(pattern in url.lower() for pattern in [
        't.me/', 'telegram.me/', 'webapp', 'startapp', 'miniapp'
    ])

async def send_alert(text):
    """Отправляет уведомление в Telegram"""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': YOUR_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")

def extract_startapp_param(url):
    """Извлекает значение параметра startapp из URL"""
    try:
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        
        if 'startapp' in query_params:
            return query_params['startapp'][0]
        else:
            match = re.search(r'startapp=([^&]+)', url)
            if match:
                return match.group(1)
        
        return None
    except Exception as e:
        print(f"❌ Ошибка извлечения параметра: {e}")
        return None

async def extract_startapp_and_find_messages():
    """Извлекает startapp параметр и находит сообщения с датами"""
    async with TelegramClient('session_messages', API_ID, API_HASH) as client:
        await client.start(PHONE)
        
        group = await client.get_entity(GROUP)
        
        today = datetime.now().date()
        messages = await client.get_messages(group, limit=30)
        print(f"📨 Проверяем сообщения с {today.strftime('%d.%m.%Y %H:%M')}")
        
        all_initial_messages = []  # Для сбора всех начальных сообщений
        all_div1_contents = []     # Для сбора содержимого первого div
        
        for i, message in enumerate(messages, 1):
            if (hasattr(message.media, 'webpage') and message.media.webpage and 
                hasattr(message.media.webpage, 'url') and message.date.date() == today):
                
                url = message.media.webpage.url
                
                if 't.me/' in url and 'startapp=' in url:                               
                    # Извлекаем параметр startapp
                    startapp_value = extract_startapp_param(url)              
                    if startapp_value:                       
                        # Формируем новый URL
                        new_url = f"https://prosto-treider.ru/qwe.php?tgWebAppStartParam={startapp_value}"
                        print(f"\n{i}. Найдена Web App ссылка: {new_url}")  
                        
                        # Добавляем URL в мониторинг
                        monitored_urls.add(startapp_value)
                        print("✅ Добавлено в мониторинг")
                        
                        # Ищем сообщения с датами и собираем их
                        div1_contents, initial_messages = await find_messages_with_dates(new_url, message.id)
                        all_div1_contents.extend(div1_contents)
                        all_initial_messages.extend(initial_messages)
                    
                    else:
                        print("❌ Не удалось извлечь startapp параметр")
        
        # Отправляем все начальные сообщения одним уведомлением
        if all_initial_messages or all_div1_contents:
            await send_initial_messages_summary(all_div1_contents, all_initial_messages)

async def find_messages_with_dates(url, message_id):
    """Находит сообщения и соответствующие даты, возвращает содержимое div1 и сообщения"""
    div1_contents = []
    initial_messages = []
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code == 200: 
            # Парсим HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            div1 = soup.find_all('div', class_='block', id=False)
            div2 = soup.find_all('div', id='msgsContent')
            if div1:      
                for idx, div in enumerate(div1, 1):
                    content = extract_div_content(div)
                    if content:
                        div1_contents.append({
                            'url': url,
                            'content': content,
                            'index': idx
                        })
            else:
                print("❌ Div без id с class='block' не найден")
            
            # Собираем сообщения из второго div
            if div2:               
                for idx, div in enumerate(div2, 1):                  
                    # Ищем все блоки сообщений
                    message_blocks = div.find_all('div', class_='msg')
                    
                    # Сохраняем последнее сообщение для этого URL
                    if message_blocks:
                        last_msg = message_blocks[-1]
                        last_messages[url] = get_message_hash(last_msg)
                    
                    # Обрабатываем каждый блок сообщения
                    for msg_idx, msg_block in enumerate(message_blocks, 1):
                        
                        # Ищем текст сообщения
                        my_text_span = msg_block.find('span', class_='my_text')
                        # Ищем дату
                        date_span = msg_block.find('span', class_='date')
                        
                        if date_span and my_text_span:
                            # Получаем информацию
                            date_text = date_span.get_text(strip=True)                       
                            message_text = my_text_span.get_text(strip=True)
                            
                            # Заменяем <br /> на переносы строк
                            message_text = message_text.replace('<br />', '\n')
                            message_text = message_text.replace('<br>', '\n')
                            
                            # Сохраняем для отправки
                            initial_messages.append({
                                'date': date_text,
                                'text': message_text,
                                'url': url
                            })
            
            else:
                print("❌ Div с id='msgsContent' не найден")
                
        else:
            print(f"❌ Ошибка сервера: {response.status_code}")
            
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    
    return div1_contents, initial_messages

def extract_div_content(div):
    """Извлекает содержимое div в читаемом формате"""
    text_content = div.get_text(strip=True)
    if text_content:
        # Убираем лишние пробелы, но сохраняем структуру
        lines = [line.strip() for line in text_content.split('\n') if line.strip()]
        return '\n'.join(lines)
    return None

async def send_initial_messages_summary(div1_contents, messages):
    """Отправляет сводку начальных сообщений и div1 содержимого одним уведомлением"""
    if not messages and not div1_contents:
        return
    
    # Формируем одно большое сообщение
    summary = f"🔗 Мониторим: {len(monitored_urls)} WebApp\n"
    
    # Добавляем содержимое первого div (если есть)
    if div1_contents:
        summary += f""
        for i, div_content in enumerate(div1_contents, 1):
            content_preview = div_content['content']
            summary += f"{content_preview}\n"
    
    # Добавляем сообщения из второго div (если есть)
    if messages:
        # Сортируем сообщения по дате (от новых к старым)
        messages.sort(key=lambda x: x['date'], reverse=True)    
        # Добавляем каждое сообщение
        for i, msg in enumerate(messages, 1):
            # Обрезаем длинные сообщения           
            summary += f"<b>💬 {i}. {msg['text']} : {msg['date']}</b>\n"
    # Отправляем одним сообщением
    await send_alert(summary)

def get_message_hash(msg_block):
    """Создает хеш сообщения для сравнения"""
    date_span = msg_block.find('span', class_='date')
    my_text_span = msg_block.find('span', class_='my_text')
    
    if date_span and my_text_span:
        date_text = date_span.get_text(strip=True)
        message_text = my_text_span.get_text(strip=True)
        return hash(date_text + message_text)
    return None

async def monitor_urls():
    """Мониторит все добавленные URL на наличие новых сообщений"""
    print(f"\n🔍 Начинаем мониторинг {len(monitored_urls)} URL...")
    
    while True:
        try:
            for url in list(monitored_urls):
                await check_for_new_messages(url)
            
            # Ждем 30 секунд перед следующей проверкой
            await asyncio.sleep(30)
            
        except Exception as e:
            print(f"❌ Ошибка в мониторинге: {e}")
            await asyncio.sleep(60)

async def check_for_new_messages(url):
    """Проверяет URL на наличие новых сообщений"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        baseUrl = "https://prosto-treider.ru/qwe.php?tgWebAppStartParam="
        response = requests.get(baseUrl + url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            div2 = soup.find_all('div', id='msgsContent')
            
            if div2:
                for div in div2:
                    message_blocks = div.find_all('div', class_='msg')
                    
                    if message_blocks:
                        current_last_msg = message_blocks[-1]
                        current_hash = get_message_hash(current_last_msg)
                        
                        # Если есть новое сообщение
                        if (url not in last_messages or 
                            last_messages[url] != current_hash):
                            
                            # Нашли новое сообщение!
                            date_span = current_last_msg.find('span', class_='date')
                            my_text_span = current_last_msg.find('span', class_='my_text')
                            
                            if date_span and my_text_span:
                                date_text = date_span.get_text(strip=True)
                                message_text = my_text_span.get_text(strip=True)
                                message_text = message_text.replace('<br />', '\n')
                                message_text = message_text.replace('<br>', '\n')
                                
                                # Отправляем уведомление
                                alert_text = f"🚨💬<b>{message_text}: {date_text}</b>"
                                await send_alert(alert_text)
                                
                                # Обновляем хеш последнего сообщения
                                last_messages[url] = current_hash
                        
    except Exception as e:
        print(f"❌ Ошибка проверки URL {url}: {e}")

async def main():
    """Основная функция"""   
    # Отправляем уведомление о запуске
    monitored_urls.add("TevHzwyLG439UXci")
    monitored_urls.add("H9T4oH7I2erd4Niv")
    
    # Создаем клиент
    client = TelegramClient('session', API_ID, API_HASH)
    
    try:
        await client.start(PHONE)
        
        # Шаг 1: Находим и анализируем сегодняшние WebApp
        await extract_startapp_and_find_messages()
        
        # Шаг 2: Запускаем мониторинг найденных URL
        if monitored_urls:
            # Отправляем уведомление о начале мониторинга
            await send_alert(f"🔍 Отслеживаю {len(monitored_urls)} WebApp")
            
            # Запускаем мониторинг в отдельной задаче
            monitor_task = asyncio.create_task(monitor_urls())
            
            # Ждем завершения (будет работать до остановки)
            await monitor_task
            
        else:
            print("❌ Не найдено WebApp для мониторинга")
            await send_alert("❌ <b>Не найдено WebApp для мониторинга</b>\n📅 Сегодняшние сообщения не обнаружены")
            
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        await send_alert(f"🔴 <b>Мониторинг остановлен с ошибкой:</b>\n{e}")
    finally:
        await client.disconnect()

# Запуск
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Мониторинг остановлен пользователем")
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")