#!/usr/bin/env python3
"""
RapidFileConvert_bot — full bot implementing "choose action first -> upload" flow,
with added PNG<->JPG conversion support.

Behavior:
 - User chooses an action first (menu button or command).
 - Bot prompts "please upload the file (as a document)" or photo.
 - User uploads -> bot converts and sends downloadable result.
 - Merge flow accumulates PDFs then "Merge Now".
"""

import os
import time
from dotenv import load_dotenv

load_dotenv()

import asyncio
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

import logging
import sqlite3
import tempfile
import shutil
import uuid
from pathlib import Path
from typing import List, Optional

from PIL import Image
from pdf2image import convert_from_path
from PyPDF2 import PdfReader, PdfWriter
from pdf2docx import Converter

import requests

from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# --- Config ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", None)
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing. Put it in a .env file or environment variables.")

MAX_UPLOAD_MB = 50
DATABASE_PATH = Path("bot_usage.db")

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger(__name__)

# --- DB helper ---
def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS usage (
               id TEXT PRIMARY KEY,
               user_id INTEGER,
               command TEXT,
               timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.commit()
    conn.close()

def log_usage(user_id: int, command: str):
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO usage (id, user_id, command) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), user_id, command))
    conn.commit()
    conn.close()

# --- Globals ---
USER_TEMP = {}     # fallback uploads saved per-user: {user_id: [{"path":..., "dir":...}, ...]}
USER_ACTIONS = {}  # pending actions per-user: {user_id: {"action": "pdf_to_word"} } or merge: {"action":"merge","files":[...]}
def bytes_to_mb(b: int) -> float:
    return b / (1024 * 1024)

def ensure_tempdir() -> Path:
    p = Path(tempfile.mkdtemp(prefix="tgfilebot_"))
    return p

def cleanup_dir(p: Path):
    try:
        shutil.rmtree(p)
    except Exception:
        pass

# --- Converters / helpers ---
def images_to_pdf(image_paths: List[Path], out_pdf: Path):
    images = []
    for p in image_paths:
        img = Image.open(p)
        if img.mode == "RGBA":
            img = img.convert("RGB")
        images.append(img)
    if not images:
        raise ValueError("No images to convert")
    images[0].save(out_pdf, save_all=True, append_images=images[1:])

def pdf_to_images(pdf_path: Path, out_dir: Path, dpi=200):
    pages = convert_from_path(str(pdf_path), dpi=dpi)
    out_paths = []
    for i, page in enumerate(pages):
        out_file = out_dir / f"page_{i + 1}.jpg"
        page.save(out_file, "JPEG")
        out_paths.append(out_file)
    return out_paths

def merge_pdfs(pdf_paths: List[Path], out_pdf: Path):
    writer = PdfWriter()
    for p in pdf_paths:
        reader = PdfReader(str(p))
        for pg in reader.pages:
            writer.add_page(pg)
    with open(out_pdf, "wb") as f:
        writer.write(f)

def compress_pdf_basic(in_pdf: Path, out_pdf: Path):
    reader = PdfReader(str(in_pdf))
    writer = PdfWriter()
    for pg in reader.pages:
        writer.add_page(pg)
    writer._info = None
    with open(out_pdf, "wb") as f:
        writer.write(f)

def docx_to_pdf_libreoffice(input_path: Path, out_pdf: Path, timeout=30):
    import subprocess
    d = out_pdf.parent
    cmd = ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(d), str(input_path)]
    subprocess.run(cmd, check=True, timeout=timeout)
    produced = d / (input_path.stem + ".pdf")
    if produced.exists():
        produced.rename(out_pdf)
    else:
        raise RuntimeError("LibreOffice conversion failed or output not found")

def pdf_to_word(pdf_path: Path, out_docx: Path):
    try:
        cv = Converter(str(pdf_path))
        cv.convert(str(out_docx), start=0, end=None)
        cv.close()
    except Exception as e:
        raise RuntimeError(f"PDF to Word conversion failed: {e}")

def convert_image_format(input_path: Path, output_path: Path, out_format: str):
    """
    Convert an image file to out_format (e.g. 'JPEG' or 'PNG').
    Handles alpha when converting to JPEG by compositing over white.
    """
    img = Image.open(input_path)
    fmt = out_format.upper()
    if fmt == "JPEG":
        # JPEG doesn't support alpha
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            alpha = img.convert("RGBA").split()[-1]
            bg.paste(img.convert("RGBA").convert("RGB"), mask=alpha)
            bg.save(output_path, format="JPEG", quality=95)
        else:
            img = img.convert("RGB")
            img.save(output_path, format="JPEG", quality=95)
    elif fmt == "PNG":
        # Preserve alpha if present
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")
        img.save(output_path, format="PNG")
    else:
        # fallback generic
        img.save(output_path, format=fmt)

# --- send file helper (async) ---
async def send_file_to_user(target, file_path: Path, caption: Optional[str] = None, filename: Optional[str] = None):
    if filename is None:
        filename = file_path.name
    try:
        if hasattr(target, "reply_document"):
            await target.reply_document(document=InputFile(str(file_path), filename=filename), caption=caption)
            return
        if hasattr(target, "message") and hasattr(target.message, "reply_document"):
            await target.message.reply_document(document=InputFile(str(file_path), filename=filename), caption=caption)
            return
    except Exception as e:
        logger.exception("Failed to send file: %s", e)
        raise

# --- UI: start/help/menu ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = "🚀 <b>Welcome to RapidFileConvert</b>"
    body = (
        "Choose a tool first using the menu below or type a command. Then upload the required file (as a document).\n\n"
        "<b>Quick commands</b>\n"
        "• <code>/menu</code> — interactive tools\n"
        "• <code>/pdf_to_word</code>\n"
        "• <code>/docx_to_pdf</code>\n"
        "• <code>/pdf_to_jpg</code>\n"
        "• <code>/jpg_to_pdf</code>\n"
        "• <code>/png_to_jpg</code>\n"
        "• <code>/jpg_to_png</code>\n"
        "• <code>/compress</code>\n"
    )
    keyboard = [
        [InlineKeyboardButton("📄 PDF → Word", callback_data="pdf_to_word"),
         InlineKeyboardButton("📝 Word → PDF", callback_data="docx_to_pdf")],
        [InlineKeyboardButton("🖼 PDF → JPG", callback_data="pdf_to_jpg"),
         InlineKeyboardButton("🟠 PNG → JPG", callback_data="png_to_jpg")],
        [InlineKeyboardButton("📷 JPG → PNG", callback_data="jpg_to_png"),
         InlineKeyboardButton("📷 JPG → PDF", callback_data="jpg_to_pdf")],
        [InlineKeyboardButton("📚 Merge PDFs", callback_data="merge"),
         InlineKeyboardButton("🗜 Compress PDF", callback_data="compress")],
        [InlineKeyboardButton("📊 Usage", callback_data="status"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    await update.message.reply_text(f"{title}\n\n{body}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>How to use</b>\n\n"
        "1) Choose an action from the menu OR type a command (e.g. /pdf_to_word).\n"
        "2) Upload the file AFTER selecting action (send as <i>document</i> or photo where appropriate).\n"
        "3) Bot converts and returns a downloadable file.\n\n"
        "Commands: /menu /pdf_to_word /docx_to_pdf /pdf_to_jpg /jpg_to_pdf /png_to_jpg /jpg_to_png /compress /merge /status"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def convert_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if hasattr(update, "callback_query") and update.callback_query:
        await update.callback_query.answer()
    kb = [
        [InlineKeyboardButton("📄 PDF → Word", callback_data="pdf_to_word"),
         InlineKeyboardButton("📝 Word → PDF", callback_data="docx_to_pdf")],
        [InlineKeyboardButton("🖼 PDF → JPG", callback_data="pdf_to_jpg"),
         InlineKeyboardButton("🟠 PNG → JPG", callback_data="png_to_jpg")],
        [InlineKeyboardButton("📷 JPG → PNG", callback_data="jpg_to_png"),
         InlineKeyboardButton("📷 JPG → PDF", callback_data="jpg_to_pdf")],
        [InlineKeyboardButton("📚 Merge PDFs", callback_data="merge"),
         InlineKeyboardButton("🗜 Compress PDF", callback_data="compress")],
        [InlineKeyboardButton("📊 Usage", callback_data="status"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    text = "<b>Choose a tool</b>\nUpload required file after selecting the tool."
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

# --- Menu callback: sets pending actions only ---
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    user_id = query.from_user.id

    if cmd == "help":
        await help_cmd(update, context); return
    if cmd == "status":
        await status(update, context); return
    if cmd == "open_menu":
        await convert_menu(update, context); return

    # Actions that require upload — only set pending action and instruct user
    if cmd in ("pdf_to_word", "docx_to_pdf", "pdf_to_jpg", "jpg_to_pdf", "compress", "merge", "png_to_jpg", "jpg_to_png"):
        if cmd == "merge":
            USER_ACTIONS[user_id] = {"action": "merge", "files": []}
            kb = [
                [InlineKeyboardButton("🔗 Merge Now", callback_data="merge_now"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
            ]
            await query.message.reply_text("📥 Merge selected. Upload PDFs now (send as documents). When ready, press Merge Now.", reply_markup=InlineKeyboardMarkup(kb))
        else:
            USER_ACTIONS[user_id] = {"action": cmd}
            await query.message.reply_text("✅ OK — please upload the file (as a document or photo depending on type). I'll convert it and send the result here.")
        return

    if cmd == "merge_now":
        action = USER_ACTIONS.get(user_id)
        if not action or action.get("action") != "merge":
            await query.message.reply_text("No merge session found. Click Merge from the menu first."); return
        pdf_paths = [Path(p) for p in action.get("files", [])]
        if len(pdf_paths) < 2:
            await query.message.reply_text("Please upload at least two PDFs before merging."); return
        out_dir = ensure_tempdir()
        out_pdf = out_dir / f"merged_{uuid.uuid4().hex}.pdf"
        try:
            merge_pdfs(pdf_paths, out_pdf)
            await send_file_to_user(query, out_pdf, caption="✅ Here is your merged PDF")
        except Exception as e:
            logger.exception("Merge failed")
            await query.message.reply_text(f"Merge failed: {e}")
        finally:
            for p in pdf_paths:
                try:
                    cleanup_dir(Path(p).parent)
                except Exception:
                    pass
            USER_ACTIONS.pop(user_id, None)
            cleanup_dir(out_dir)
        return

    if cmd == "cancel_action":
        USER_ACTIONS.pop(user_id, None)
        await query.message.reply_text("Action cancelled.")
        return

    await query.message.reply_text("Unknown action. Try /menu.")

# --- Command handlers that set pending action (user must upload after) ---
async def pdf_to_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "pdf_to_word"}
    await update.message.reply_text("PDF → Word selected. Please upload the PDF (send as a document).")

async def docx_to_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "docx_to_pdf"}
    await update.message.reply_text("Word → PDF selected. Please upload the DOCX (send as a document).")

async def pdf_to_jpg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "pdf_to_jpg"}
    await update.message.reply_text("PDF → JPG selected. Please upload the PDF (send as a document).")

async def jpg_to_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "jpg_to_pdf"}
    await update.message.reply_text("JPG → PDF selected. Please upload the image (photo or document).")

async def png_to_jpg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "png_to_jpg"}
    await update.message.reply_text("PNG → JPG selected. Please upload the PNG image (photo or document).")

async def jpg_to_png_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "jpg_to_png"}
    await update.message.reply_text("JPG → PNG selected. Please upload the JPG/JPEG image (photo or document).")

async def compress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "compress"}
    await update.message.reply_text("Compress selected. Please upload the PDF (send as a document).")

async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_ACTIONS[uid] = {"action": "merge", "files": []}
    await update.message.reply_text("Merge selected. Upload multiple PDFs (send as documents), then press Merge Now in the menu or use /menu -> Merge Now.")

# --- Status ---
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM usage WHERE user_id = ?", (user_id,))
    count = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Your usage count: {count}")

# --- Document handler: requires pending action first ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    log_usage(user_id, "upload")
    message = update.message
    tempdir = ensure_tempdir()

    file_obj = await message.document.get_file()
    fname = message.document.file_name or f"file_{uuid.uuid4().hex}"
    dest = tempdir / fname

    await update.message.reply_text("Downloading file...")
    await file_obj.download_to_drive(custom_path=str(dest))

    size_bytes = dest.stat().st_size
    if bytes_to_mb(size_bytes) > MAX_UPLOAD_MB:
        cleanup_dir(tempdir)
        await update.message.reply_text(f"File too large. Max allowed is {MAX_UPLOAD_MB} MB.")
        return

    action_info = USER_ACTIONS.get(user_id)

    if not action_info:
        # store but insist user choose action first
        USER_TEMP.setdefault(user_id, []).append({"path": str(dest), "dir": str(tempdir)})
        await update.message.reply_text("I received your file — but first please choose what you want me to do. Use /menu or type a command (e.g. 'pdf to word').")
        return

    # merge accumulate
    if action_info.get("action") == "merge":
        if not str(dest).lower().endswith(".pdf"):
            cleanup_dir(tempdir)
            await update.message.reply_text("Only PDF files are accepted for merging. Please upload PDFs.")
            return
        action_info.setdefault("files", []).append(str(dest))
        USER_ACTIONS[user_id] = action_info
        await update.message.reply_text("📥 PDF saved for merging. Upload more files or press Merge Now in the menu.")
        return

    # perform action on the uploaded single file
    act = action_info.get("action")
    try:
        if act == "pdf_to_word":
            if not str(dest).lower().endswith(".pdf"):
                await update.message.reply_text("Please upload a PDF for PDF→Word conversion.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            out_docx = out_dir / (Path(dest).stem + ".docx")
            await update.message.reply_text("⏳ Converting PDF → Word (DOCX)...")
            pdf_to_word(Path(dest), out_docx)
            await send_file_to_user(update.message, out_docx, caption="✅ PDF converted to Word (DOCX)")
            cleanup_dir(out_dir)

        elif act == "docx_to_pdf":
            if not str(dest).lower().endswith((".docx", ".doc")):
                await update.message.reply_text("Please upload a DOCX file for Word→PDF conversion.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            out_pdf = out_dir / (Path(dest).stem + ".pdf")
            await update.message.reply_text("⏳ Converting Word → PDF (LibreOffice)...")
            docx_to_pdf_libreoffice(Path(dest), out_pdf)
            await send_file_to_user(update.message, out_pdf, caption="✅ DOCX converted to PDF")
            cleanup_dir(out_dir)

        elif act == "pdf_to_jpg":
            if not str(dest).lower().endswith(".pdf"):
                await update.message.reply_text("Please upload a PDF for PDF→JPG conversion.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            await update.message.reply_text("⏳ Converting PDF → JPG pages...")
            pages = pdf_to_images(Path(dest), out_dir)
            for p in pages:
                await update.message.reply_document(document=InputFile(str(p)), caption="Page image")
            await update.message.reply_text("✅ Done — sent all page images.")
            cleanup_dir(out_dir)

        elif act == "jpg_to_pdf":
            if not any(str(dest).lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff")):
                await update.message.reply_text("Please upload an image for JPG→PDF conversion.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            out_pdf = out_dir / (Path(dest).stem + ".pdf")
            await update.message.reply_text("⏳ Converting image → PDF...")
            images_to_pdf([Path(dest)], out_pdf)
            await send_file_to_user(update.message, out_pdf, caption="✅ Image converted to PDF")
            cleanup_dir(out_dir)

        elif act == "png_to_jpg":
            if not any(str(dest).lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff")):
                await update.message.reply_text("Please upload an image (PNG preferred) for PNG→JPG conversion.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            out_file = out_dir / (Path(dest).stem + ".jpg")
            await update.message.reply_text("⏳ Converting PNG → JPG...")
            convert_image_format(Path(dest), out_file, "JPEG")
            await send_file_to_user(update.message, out_file, caption="✅ PNG converted to JPG", filename=out_file.name)
            cleanup_dir(out_dir)

        elif act == "jpg_to_png":
            if not any(str(dest).lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff")):
                await update.message.reply_text("Please upload an image (JPG preferred) for JPG→PNG conversion.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            out_file = out_dir / (Path(dest).stem + ".png")
            await update.message.reply_text("⏳ Converting JPG → PNG...")
            convert_image_format(Path(dest), out_file, "PNG")
            await send_file_to_user(update.message, out_file, caption="✅ JPG converted to PNG", filename=out_file.name)
            cleanup_dir(out_dir)

        elif act == "compress":
            if not str(dest).lower().endswith(".pdf"):
                await update.message.reply_text("Please upload a PDF to compress.")
                cleanup_dir(tempdir); return
            out_dir = ensure_tempdir()
            out_pdf = out_dir / f"compressed_{Path(dest).name}"
            await update.message.reply_text("⏳ Compressing PDF...")
            compress_pdf_basic(Path(dest), out_pdf)
            await send_file_to_user(update.message, out_pdf, caption="✅ Compressed PDF")
            cleanup_dir(out_dir)

        else:
            await update.message.reply_text("Unknown action. Use /menu.")
    except Exception as e:
        logger.exception("Conversion failed")
        await update.message.reply_text(f"Conversion failed: {e}")
    finally:
        USER_ACTIONS.pop(user_id, None)
        cleanup_dir(tempdir)

# --- Photo handler: requires pending action (png/jpg->other or jpg->pdf) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    log_usage(user_id, "photo")
    tempdir = ensure_tempdir()
    photo = update.message.photo[-1]
    file_obj = await photo.get_file()
    dest = tempdir / f"photo_{uuid.uuid4().hex}.jpg"
    await update.message.reply_text("Downloading photo...")
    await file_obj.download_to_drive(custom_path=str(dest))

    action_info = USER_ACTIONS.get(user_id)
    if not action_info:
        cleanup_dir(tempdir)
        await update.message.reply_text("I received your photo, but please choose an action first (Use /menu).")
        return

    act = action_info.get("action")
    try:
        if act == "jpg_to_pdf" or act == "jpg_to_png" or act == "png_to_jpg":
            # photo saved as JPEG; convert accordingly
            out_dir = ensure_tempdir()
            if act == "jpg_to_pdf":
                out_pdf = out_dir / (Path(dest).stem + ".pdf")
                await update.message.reply_text("⏳ Converting photo → PDF...")
                images_to_pdf([Path(dest)], out_pdf)
                await send_file_to_user(update.message, out_pdf, caption="✅ Image converted to PDF")
            elif act == "jpg_to_png":
                out_file = out_dir / (Path(dest).stem + ".png")
                await update.message.reply_text("⏳ Converting JPG → PNG...")
                convert_image_format(Path(dest), out_file, "PNG")
                await send_file_to_user(update.message, out_file, caption="✅ JPG converted to PNG")
            elif act == "png_to_jpg":
                out_file = out_dir / (Path(dest).stem + ".jpg")
                await update.message.reply_text("⏳ Converting PNG → JPG...")
                convert_image_format(Path(dest), out_file, "JPEG")
                await send_file_to_user(update.message, out_file, caption="✅ PNG converted to JPG")
            cleanup_dir(out_dir)
        else:
            await update.message.reply_text("This upload does not match your pending action. Use /menu to choose the correct tool.")
    except Exception as e:
        logger.exception("Photo conversion failed")
        await update.message.reply_text(f"Conversion failed: {e}")
    finally:
        USER_ACTIONS.pop(user_id, None)
        cleanup_dir(tempdir)

# --- simple text router for phrases (optional) ---
async def text_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    t = update.message.text.strip().lower()
    mapping = {
        "pdf to word": pdf_to_word_command,
        "pdf to jpg": pdf_to_jpg_command,
        "jpg to pdf": jpg_to_pdf_command,
        "png to jpg": png_to_jpg_command,
        "jpg to png": jpg_to_png_command,
        "docx to pdf": docx_to_pdf_command,
        "merge": merge_command,
        "compress": compress_command,
        "menu": convert_menu,
    }
    if t in mapping:
        await mapping[t](update, context)
        return
    # fallback: ignore / suggest menu
    return

# --- unknown handler ---
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("I didn't understand that. Use /menu to choose an action first.")

# --- main with retry init ---
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("menu", convert_menu))
    app.add_handler(CommandHandler("pdf_to_word", pdf_to_word_command))
    app.add_handler(CommandHandler("docx_to_pdf", docx_to_pdf_command))
    app.add_handler(CommandHandler("pdf_to_jpg", pdf_to_jpg_command))
    app.add_handler(CommandHandler("jpg_to_pdf", jpg_to_pdf_command))
    app.add_handler(CommandHandler("png_to_jpg", png_to_jpg_command))
    app.add_handler(CommandHandler("jpg_to_png", jpg_to_png_command))
    app.add_handler(CommandHandler("compress", compress_command))
    app.add_handler(CommandHandler("merge", merge_command))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(CallbackQueryHandler(menu_callback))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_command_router))

    app.add_handler(MessageHandler(filters.ALL, unknown))

    MAX_INIT_RETRIES = 6
    INIT_BACKOFF = 2

    logger.info("Initializing application (with retry)...")
    loop = asyncio.get_event_loop()
    retries = 0
    while True:
        try:
            loop.run_until_complete(app.initialize())
            logger.info("Application initialized.")
            break
        except Exception as e:
            retries += 1
            logger.exception(f"Initialize attempt {retries} failed: {e!r}")
            try:
                import socket
                logger.info("DNS resolution test: api.telegram.org -> %s", socket.gethostbyname("api.telegram.org"))
            except Exception as dns_e:
                logger.warning("DNS resolution failed: %s", dns_e)
            if retries >= MAX_INIT_RETRIES:
                logger.error("Exceeded max init retries — exiting.")
                raise
            wait = INIT_BACKOFF * (2 ** (retries - 1))
            logger.info(f"Retrying initialize after {wait} seconds (attempt {retries}/{MAX_INIT_RETRIES})...")
            time.sleep(wait)

    logger.info("Starting polling now.")
    app.run_polling()

if __name__ == "__main__":
    main()
