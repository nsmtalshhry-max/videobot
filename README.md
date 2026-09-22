# بوت تحميل الفيديو - المرحلة 1

## 1) إنشاء البوت
1. افتح @BotFather في تليجرام وأرسل /newbot
2. اختر الاسم واليوزر (لازم ينتهي بـ bot)
3. انسخ التوكن

## 2) التجربة على جهازك
1. ثبّت Python 3.10+ وثبّت ffmpeg:
   - ويندوز: `winget install ffmpeg`
   - ماك: `brew install ffmpeg`
   - لينكس: `sudo apt install ffmpeg`
2. داخل مجلد المشروع:
   ```
   python -m venv venv
   source venv/bin/activate      # ويندوز: venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env          # ثم افتحه وحط التوكن
   python bot.py
   ```
3. افتح البوت في تليجرام وأرسل /start ثم رابط فيديو.

## 3) النشر على Oracle Cloud (Always Free)
1. أنشئ VM من نوع Ubuntu (Always Free) وادخل عليها بـ SSH
2. ثبّت Docker:
   ```
   sudo apt update && sudo apt install -y docker.io
   sudo usermod -aG docker $USER   # ثم اخرج وادخل مرة ثانية
   ```
3. انسخ مجلد المشروع للسيرفر (scp أو git) وأنشئ ملف .env فيه التوكن
4. شغّله:
   ```
   docker build -t videobot .
   docker run -d --name videobot --restart unless-stopped --env-file .env videobot
   ```
5. عرض السجلات: `docker logs -f videobot`

## 4) صيانة مهمة
- تحديث yt-dlp كل فترة (المنصات تغيّر نظامها):
  `docker build --no-cache -t videobot . && docker rm -f videobot && docker run -d --name videobot --restart unless-stopped --env-file .env videobot`
- إذا يوتيوب طلب تسجيل دخول: صدّر cookies من متصفحك (إضافة "Get cookies.txt LOCALLY")
  وضع الملف باسم cookies.txt جنب bot.py، ولو تشغل بدوكر أضف
  `-v $(pwd)/cookies.txt:/app/cookies.txt` لأمر docker run.
  استخدم حساب ثانوي مو حسابك الرئيسي.
