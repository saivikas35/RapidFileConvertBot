# RapidPDFTools Bot

This is a simple Telegram file-conversion bot (MVP). Features:
- JPG -> PDF
- PDF -> JPG (requires poppler)
- Merge multiple PDFs
- Compress PDF (basic)
- Word <-> PDF (LibreOffice)

## Setup (local)
1. Clone or extract the project.
2. Install Python 3.10+ and VS Code (optional).
3. Create and activate virtualenv:
   - Windows:
     ```
     python -m venv venv
     venv\Scripts\activate
     ```
   - Mac/Linux:
     ```
     python3 -m venv venv
     source venv/bin/activate
     ```
4. Install dependencies:
   ```
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
5. Install system dependencies:
   - Poppler (for pdf2image)
     - Ubuntu: `sudo apt-get install -y poppler-utils`
     - Mac: `brew install poppler`
   - LibreOffice (optional for .docx conversion)
6. Set TELEGRAM_BOT_TOKEN environment variable:
   - Windows (PowerShell): `setx TELEGRAM_BOT_TOKEN "YOUR_TOKEN_HERE"`
   - Mac/Linux: `export TELEGRAM_BOT_TOKEN="YOUR_TOKEN_HERE"`
   Restart your terminal after using setx on Windows.
7. (Optional) Register bot commands:
   ```
   python bot_setup.py
   ```
8. Run:
   ```
   python bot.py
   ```

## Docker
Build:
```
docker build -t rapid-pdf-bot .
```
Run:
```
docker run -e TELEGRAM_BOT_TOKEN="YOUR_TOKEN" rapid-pdf-bot
```

## Next steps
- Add subscription/payment gating (Stripe/Razorpay)
- Deploy to Railway / Render with webhook mode for 24/7 uptime
- Store files in S3 and serve presigned download links
