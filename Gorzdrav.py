import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import requests
import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll

# Настройки
SUBSCRIBERS = [
    124970852,
    #227899871
]
API_URL_2 = 'https://gorzdrav.spb.ru/_api/api/v2/referral/78253057083102?lastName=%D0%9E%D1%82%D1%80%D0%B0%D1%88%D0%B5%D0%B2%D1%81%D0%BA%D0%B0%D1%8F'  #
API_URL_SPEC = 'https://gorzdrav.spb.ru/_api/api/v2/schedule/lpu/212/specialties'  #
        
def send_vk_message(user_id, message):
    """Отправляет сообщение конкретному пользователю/группе"""
    token = "vk1.a.NtlIW2sPiZihemQK3FFq7gHleSEHsQW-WDnbZMJgXdY_PqjP169r5T_74y_Lmi8NUK_EWNuFedzjnSqodMcd3emNMjF0kvra2qoztMSOo_l6axkfDIKMBFO2B5mPJT6963nFl8TBpF6JqKSg4qYlw4qG2zIE2bxxNZzIRVuaSTMJhzWtoPPzyislISWVfyOu51jmqSH6hKiSZqhfbvomOg"
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        vk.messages.send(user_id=user_id, message=message, random_id=0)
        return True

    except Exception as e:
        print(f"Ошибка отправки для {user_id}: {e}")
        return False

def send_to_all_subscribers(message):
    """Отправляет сообщение всем подписчикам"""
    success_count = 0
    for chat_id in SUBSCRIBERS:
        #if send_telegram_message(chat_id, message):
        if send_vk_message(chat_id, message):
            success_count += 1
            print(f"✅ Отправлено для {chat_id}")
        else:
            print(f"❌ Ошибка отправки для {chat_id}")
        time.sleep(0.5)  # Чтобы не превысить лимиты Telegram
    return success_count

def check_appointments(doctorId, lpuId):
    """Проверяет доступные записи"""
    url = 'https://gorzdrav.spb.ru/_api/api/v2/schedule/lpu/'+ lpuId + '/doctor/' + doctorId + '/appointments'
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        if data.get('success') and data.get('result'):
            appointments = data['result']
            now = datetime.now()
           
            
            future_appointments = [
                app for app in appointments 
                if datetime.fromisoformat(app['visitStart']) > now
            ]
            
            
            if future_appointments:
                
                nearest = min(future_appointments, 
                            key=lambda x: datetime.fromisoformat(x['visitStart']))
                
                
                visit_time = datetime.fromisoformat(nearest['visitStart'])
                formatted_time = visit_time.strftime('%d.%m.%Y в %H:%M')
                
                message = f"🚨 НАЙДЕНА СВОБОДНАЯ ЗАПИСЬ! 🚨\n\n"
                message += f"📅 Дата: {formatted_time}\n"
                message += f"🏥 Адрес: {nearest.get('address', 'Не указан')}\n"
                message += f"🚪 Кабинет: {nearest.get('room', 'Не указан')}\n\n"
                message += f"📍 Ссылка: https://gorzdrav.spb.ru/service-free-schedule#%5B%7B%22district%22:%2215%22%7D,%7B%22lpu%22:%22224%22%7D,%7B%22speciality%22:%2235%22%7D%5D"
                
                return message, nearest.get('id')
            else:
                return None, None
        else:
            return None, None
            
    except Exception as e:
        print(f"Ошибка проверки: {e}")
        return None, None
        
        
def check(doctorId, lpuId, last_found_ids):
    message, app_id = check_appointments(doctorId, lpuId)
             
    if message and app_id and app_id not in last_found_ids: 
        print(message)            
        success_count = send_to_all_subscribers(message)
        print(f"📊 Отправлено {success_count}/{len(SUBSCRIBERS)} уведомлений")
        
        last_found_ids.add(app_id)
        # Очищаем старые ID чтобы не копить бесконечно
        if len(last_found_ids) > 20:
            last_found_ids.clear()

def main():
    print("🚀 Мониторинг для нескольких пользователей запущен!")
    print(f"👥 Подписчиков: {len(SUBSCRIBERS)}")
    print("⏰ Проверка каждые 30 секунд...")
    
    doctorIds = [('221', '%D0%BF30%2e397'), ('224', '52')]  #('221', '%D0%B4%D0%BF4%2e33')
    
    last_found_ids = set()
    
    try:
        while True:
            for doctorId in doctorIds:
                check(doctorId[1], doctorId[0], last_found_ids)        
            
            time.sleep(30)
            
    except KeyboardInterrupt:
        print("\n👋 Мониторинг остановлен")

if __name__ == "__main__":
    main()