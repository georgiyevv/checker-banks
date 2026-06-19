import asyncio
import os
import glob
import logging
import tempfile
import zipfile

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from main import check_receipt, DedupStore, VERDICT_RU

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _dedup_path(user_id: int) -> str:
    return os.path.join(BASE_DIR, f"dedup_{user_id}.json")


def _user_results(context: ContextTypes.DEFAULT_TYPE) -> list:
    if "results" not in context.user_data:
        context.user_data["results"] = []
    return context.user_data["results"]


def _format_verdict(name: str, res: dict) -> str:
    status  = res["status"]
    emoji   = {"CLEAN": "✅", "SUSPICIOUS": "⚠️", "REJECTED": "❌"}.get(status, "❓")
    verdict = VERDICT_RU.get(status, status)

    lines = [f"{emoji} {verdict}  —  {name}", f"   {res['advice']}"]

    for f in res.get("hard_fails", []):
        lines.append(f"   🔴 {f}")
    for f in res.get("soft_flags", []):
        lines.append(f"   🟡 {f}")

    fields = res.get("fields", {})
    parts = []
    if fields.get("datetime_visible"):
        parts.append(f"дата {fields['datetime_visible']}")
    if fields.get("total") is not None:
        parts.append(f"сумма {fields['total']} ₽")
    if fields.get("operation_id"):
        parts.append(f"ID {fields['operation_id']}")
    if fields.get("phone"):
        parts.append(f"тел. {fields['phone']}")
    if parts:
        lines.append("   ℹ️ " + " | ".join(parts))

    return "\n".join(lines)


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот проверки чеков Озон Банка\n"
        "\n"
        "Как пользоваться:\n"
        "• Отправьте ZIP-архив с PDF-чеками\n"
        "• Или отправьте один PDF\n"
        "\n"
        "Команды:\n"
        "/report - отчёт по всем проверенным PDF за сессию\n"
        "/reset  - сбросить историю\n"
        "/help   - справка"
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправьте ZIP-архив с чеками Ozon Банка в формате PDF.\n"
        "Бот извлечёт все PDF, проверит каждый и пришлёт отчёт файлом.\n"
        "\n"
        "✅ - признаков подделки нет\n"
        "⚠️ - подозрительный чек\n"
        "❌ - документ подделан или уже использован\n"
        "\n"
        "Дубликаты определяются автоматически внутри архива и между сессиями."
    )


# ── /otchet ───────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = _user_results(context)
    if not results:
        await update.message.reply_text(
            "Нет проверенных файлов.\n"
            "Отправьте ZIP-архив с чеками или отдельный PDF."
        )
        return

    n_valid = sum(1 for _, r in results if r["status"] == "CLEAN")
    n_susp  = sum(1 for _, r in results if r["status"] == "SUSPICIOUS")
    n_fake  = sum(1 for _, r in results if r["status"] == "REJECTED")

    await update.message.reply_text(
        f"Итого проверено: {len(results)} файлов\n"
        f"✅ Валидных: {n_valid}\n"
        f"⚠️ Подозрительных: {n_susp}\n"
        f"❌ Фейков: {n_fake}"
    )


# ── /sbros ────────────────────────────────────────────────────────────────────

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["results"] = []
    path = _dedup_path(update.effective_user.id)
    if os.path.exists(path):
        os.remove(path)
    await update.message.reply_text("История очищена. Можно загружать новые чеки.")


# ── ZIP ───────────────────────────────────────────────────────────────────────

async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document

    msg = await update.message.reply_text("Получил архив, извлекаю файлы…")

    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "archive.zip")
        await tg_file.download_to_drive(zip_path)

        # распаковка
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile:
            await msg.edit_text("Не удалось распаковать архив. Убедитесь, что файл в формате ZIP.")
            return

        pdf_paths = sorted(glob.glob(os.path.join(tmpdir, "**", "*.pdf"), recursive=True))
        if not pdf_paths:
            await msg.edit_text("В архиве не найдено PDF-файлов.")
            return

        await msg.edit_text(f"Найдено {len(pdf_paths)} PDF. Проверяю…")

        user_id = update.effective_user.id
        dedup   = DedupStore(path=_dedup_path(user_id))
        results = []

        for path in pdf_paths:
            name = os.path.relpath(path, tmpdir)
            try:
                res = check_receipt(path, dedup=dedup)
                dedup.register(path, res.get("fields", {}), label=name)
            except Exception as exc:
                res = {
                    "status": "REJECTED",
                    "advice": "Файл не удалось разобрать как PDF.",
                    "hard_fails": [f"ошибка разбора: {exc}"],
                    "soft_flags": [],
                    "fields": {},
                }
            results.append((name, res))

        _user_results(context).extend(results)

    await msg.edit_text(
        f"Проверка завершена: {len(results)} файлов.\n"
        f"✅ Валидных: {sum(1 for _, r in results if r['status'] == 'CLEAN')}\n"
        f"⚠️ Подозрительных: {sum(1 for _, r in results if r['status'] == 'SUSPICIOUS')}\n"
        f"❌ Фейков: {sum(1 for _, r in results if r['status'] == 'REJECTED')}"
    )


# ── одиночный PDF ─────────────────────────────────────────────────────────────

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    msg = await update.message.reply_text("Проверяю…")

    user_id = update.effective_user.id
    dedup   = DedupStore(path=_dedup_path(user_id))

    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        res = check_receipt(tmp_path, dedup=dedup)
        dedup.register(tmp_path, res.get("fields", {}), label=doc.file_name)
        _user_results(context).append((doc.file_name, res))
        await msg.edit_text(_format_verdict(doc.file_name, res))
    except Exception as e:
        logging.exception("Ошибка при обработке %s", doc.file_name)
        await msg.edit_text(f"Ошибка обработки файла: {e}")
    finally:
        os.unlink(tmp_path)


# ── прочие файлы ──────────────────────────────────────────────────────────────

async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Принимаю:\n"
        "• ZIP-архив с PDF-чеками — для пакетной проверки\n"
        "• Отдельный PDF-файл — для быстрой проверки"
    )


# ── отправка отчёта ──────────────────────────────────────────────────────────

async def _send_report(chat_id: int, user_id: int, results: list, context: ContextTypes.DEFAULT_TYPE):
    n_valid = sum(1 for _, r in results if r["status"] == "CLEAN")
    n_susp  = sum(1 for _, r in results if r["status"] == "SUSPICIOUS")
    n_fake  = sum(1 for _, r in results if r["status"] == "REJECTED")

    lines = [f"Отчёт по {len(results)} файлам:", ""]
    for name, res in results:
        status  = res["status"]
        emoji   = {"CLEAN": "✅", "SUSPICIOUS": "⚠️", "REJECTED": "❌"}.get(status, "❓")
        verdict = VERDICT_RU.get(status, status)
        lines.append(f"{emoji} {verdict}  —  {name}")

    lines += [
        "",
        f"✅ Валидных: {n_valid}",
        f"⚠️ Подозрительных: {n_susp}",
        f"❌ Фейков: {n_fake}",
    ]

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",                cmd_start))
    app.add_handler(CommandHandler(["help", "pomosh"],     cmd_help))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("reset",  cmd_reset))

    # ZIP-архив
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("zip"),
        handle_zip,
    ))
    # одиночный PDF
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    # всё остальное
    app.add_handler(MessageHandler(filters.Document.ALL, handle_other))

    logging.info("Бот запущен")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
