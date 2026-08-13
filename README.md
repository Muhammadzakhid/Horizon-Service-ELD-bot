# ELD xavfsizlik va ogohlantirish Telegram boti

Bot ELD tizimidan webhook orqali signal qabul qilib,
dispetcher va menejerlarning Telegram chatlariga tayyor ALERT yuboradi.

Kompaniya bo‘yicha webhook manzillari:

- `POST /webhook/7sky` — kompaniya nomi doim `7SKY LOGISTICS INC`;
- `POST /webhook/msv` — kompaniya nomi doim `MSV TRANSPORT LLC`;
- `POST /webhook/alert` — eski umumiy manzil, `company_name` so‘rovdan olinadi.

7SKY va MSV integratsiyalarini tegishli kompaniya manziliga sozlash kerak.

## Ishga tushirish

1. Telegram’da `@BotFather` orqali bot oching va token oling.
2. Kutubxonalarni o‘rnating: `pip install -r requirements.txt`
3. `.env.example` faylidan `.env` nusxa yarating va haqiqiy qiymatlarni kiriting.
4. Botni ishga tushiring: `python app.py`
5. Botni kerakli guruhga qo‘shib, guruhda `/start` yuboring.

Alertlarni ikkita muayyan manzilga yuborish uchun `.env` ichida `CHAT_ID_1` va
`CHAT_ID_2` belgilang. Masalan, `CHAT_ID_1=-1001234567890`. Ular sozlangan
bo‘lsa, alertlar shu chatlarga boradi va `/start` bilan obuna bo‘lish shart
emas. Kanalda botga post yuborish huquqi berilishi kerak.

Bot buyruqlari:

- `/start` — joriy chatni ALERT oluvchilar ro‘yxatiga qo‘shadi.
- `/stop` — joriy chatni ro‘yxatdan chiqaradi.
- `/status` — obuna holati va oluvchilar sonini ko‘rsatadi.

Telegram ulanishi uchun HTTPX connect/read timeout qiymatlari 30 soniya. Botning
boshlang‘ich Telegram API ulanishi vaqtincha ishlamasa, `bootstrap_retries=-1`
sabab avtomatik qayta urinadi. `.env` orqali `TELEGRAM_CONNECT_TIMEOUT`,
`TELEGRAM_READ_TIMEOUT` va `TELEGRAM_BOOTSTRAP_RETRIES` o‘zgartirilishi mumkin.

Server holatini `GET /health` orqali tekshirish mumkin.
U token, webhook himoyasi va subscriber mavjudligini ham ko‘rsatadi. Tayyor bo‘lmasa
`503 degraded` qaytaradi.

## Webhook formati

```http
POST /webhook/7sky
Content-Type: application/json
X-Webhook-Secret: super-secret-key-2026
```

```json
{
  "scenario": "weigh_station",
  "driver_name": "John Smith",
  "truck_unit": "Unit-4521",
  "location": "I-80 W, Cheyenne, WY",
  "time": "2026-08-14 09:42 MST"
}
```

`scenario` qiymati quyidagilardan biri bo‘lishi kerak:

- `weigh_station` — weigh station’gacha 20 mil qolganda;
- `log_frozen` — logbook yangilanmay qolganda;
- `driver_disconnected` — haydovchi ELD’dan uzilganda.

Bot tashqi tizimlarda ko‘p uchraydigan `eventType`, `driverName`, `unitNumber`,
`currentLocation`, `eventTime` va ichki `data` obyektini ham avtomatik taniydi.
`eventId` yoki `alertId` yuborilsa, bir hodisaning takror webhooklari besh daqiqa
ichida Telegram’ga qayta yuborilmaydi.

Majburiy ALERT maydonlaridan birortasi yuborilmasa yoki bo‘sh bo‘lsa, bot uning
o‘rniga `N/A` yozadi. `time` qiymatida aniq vaqt va vaqt zonasi ELD tizimi
tomonidan yuborilishi kerak.

`/webhook/7sky` va `/webhook/msv` uchun JSON ichida `company_name` yuborish shart
emas. Yuborilgan taqdirda ham bot xavfsiz marshrutlash uchun uni endpointga
biriktirilgan rasmiy kompaniya nomi bilan almashtiradi.

## Sinov

```bash
python -m unittest -v
```

## Production

Webhook manzilini internetdan foydalanish uchun HTTPS reverse proxy yoki hosting
orqali oching. `.env` ichida kuchli `WEBHOOK_SECRET` belgilang va uni ELD
so‘rovlarida `X-Webhook-Secret` sarlavhasi bilan yuboring. `.env` faylini git’ga
joylamang.

`sevensky_api` va `msv_api` kalitlari ELD servisidan faol ravishda ma’lumot olish
uchun bo‘lsa, servisning API base URL’i, endpointlari va javob namunasi ham kerak.
Webhook qabul qilishning o‘zi bu kalitlar bilan avtomatik polling qilmaydi.
