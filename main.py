import re
import os
import json
import zlib
import struct
import hashlib
import datetime as dt


GENUINE_BOLD_FONT_HASH = "03086a5e44"
GENUINE_IMAGE_HASHES = {"1cda6fb8", "a697086a"}
GENUINE_PAGE_WIDTH = "373.91998"
GENUINE_PRODUCER = "Skia/PDF m105"
GENUINE_CREATOR = "Chromium"
REQUIRED_MARKERS = ["озон банк", "инн 9703077050", "бик 044525068"]
MSK_OFFSET_HOURS = 3


_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj(.*?)endobj", re.DOTALL)


class PdfDocument:

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.objects = self._parse_objects()

    def _parse_objects(self):
        out = {}
        for m in _OBJ_RE.finditer(self.data):
            num = int(m.group(1))
            body = m.group(3)
            s = body.find(b"stream")
            if s < 0:
                out[num] = (body, None)
                continue
            start = s + 6
            if body[start:start + 2] == b"\r\n":
                start += 2
            elif body[start:start + 1] in (b"\n", b"\r"):
                start += 1
            end = body.rfind(b"endstream")
            out[num] = (body[:s], body[start:end])
        return out

    def info(self, key):
        m = re.search(rb"/" + key.encode() + rb"\s*\(([^)]*)\)", self.data)
        return m.group(1).decode("latin1") if m else None

    @property
    def version(self):
        return self.data[:8].decode("latin1", "replace").strip()

    @property
    def page_width(self):
        m = re.search(rb"/MediaBox\s*\[([^\]]*)\]", self.data)
        if not m:
            return None
        parts = m.group(1).split()
        return parts[2].decode() if len(parts) >= 3 else None

    @property
    def eof_count(self):
        return self.data.count(b"%%EOF")

    @property
    def has_incremental_update(self):
        return b"/Prev" in self.data or self.eof_count > 1

    def images(self):
        result = []
        for header, stream in self.objects.values():
            if stream is None or b"/Image" not in header:
                continue
            try:
                raw = zlib.decompress(stream)
            except zlib.error:
                raw = stream
            w = re.search(rb"/Width\s+(\d+)", header)
            h = re.search(rb"/Height\s+(\d+)", header)
            result.append({
                "hash": hashlib.md5(raw).hexdigest()[:8],
                "width": int(w.group(1)) if w else None,
                "height": int(h.group(1)) if h else None,
            })
        return result

    def fonts(self):
        fonts = {}
        for header, _ in self.objects.values():
            ref = re.search(rb"/FontFile2\s+(\d+)\s+0\s+R", header)
            name = re.search(rb"/FontName\s*/([A-Za-z0-9+,_-]+)", header)
            if not ref or not name:
                continue
            ff_header, ff_stream = self.objects.get(int(ref.group(1)), (None, None))
            if ff_stream is None:
                continue
            try:
                ttf = zlib.decompress(ff_stream)
            except zlib.error:
                ttf = ff_stream
            full = name.group(1).decode()
            prefix, _, style = full.partition("+")
            fonts[style or full] = {
                "prefix": prefix,
                "hash": hashlib.md5(ttf).hexdigest()[:10],
                "num_glyphs": _num_glyphs(ttf),
            }
        return fonts

    def text(self):
        union = {}
        content = None
        for header, stream in self.objects.values():
            if stream is None:
                continue
            try:
                dec = zlib.decompress(stream)
            except zlib.error:
                continue
            if b"beginbfchar" in dec or b"beginbfrange" in dec:
                union.update(_parse_tounicode(dec))
            if b"BT" in dec and b"Tj" in dec and (content is None or len(dec) > len(content)):
                content = dec
        if content is None:
            return ""
        txt = content.decode("latin1")
        out = []
        for mm in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj|<([0-9A-Fa-f]+)>\s*Tj", txt):
            if mm.group(1) is not None:
                b = bytes(mm.group(1), "latin1").decode("unicode_escape").encode("latin1")
            else:
                hx = mm.group(2)
                b = bytes(int(hx[i:i + 2], 16) for i in range(0, len(hx), 2))
            out.append("".join(
                union.get(b[i] << 8 | (b[i + 1] if i + 1 < len(b) else 0), "?")
                for i in range(0, len(b), 2)
            ))
        return "".join(out)


def _num_glyphs(ttf):
    if len(ttf) < 12:
        return None
    try:
        num_tables = struct.unpack(">H", ttf[4:6])[0]
        off = 12
        for _ in range(num_tables):
            tag = ttf[off:off + 4]
            toff = struct.unpack(">I", ttf[off + 8:off + 12])[0]
            if tag == b"maxp":
                return struct.unpack(">H", ttf[toff + 4:toff + 6])[0]
            off += 16
    except struct.error:
        return None
    return None


def _parse_tounicode(dec):
    txt = dec.decode("latin1")
    m = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.DOTALL):
        for s, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            m[int(s, 16)] = "".join(chr(int(d[i:i + 4], 16)) for i in range(0, len(d), 4))
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.DOTALL):
        for lo, hi, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            lo, hi, base = int(lo, 16), int(hi, 16), int(d, 16)
            for i, code in enumerate(range(lo, hi + 1)):
                m[code] = chr(base + i)
    return m


def extract_fields(text):
    def grab(pat):
        m = re.search(pat, text)
        return m.group(1).strip() if m else None

    def amount(label):
        m = re.search(label + r"([\d\s  ]+?)\s*₽", text)
        return int(re.sub(r"\D", "", m.group(1))) if m else None

    dt_raw = grab(r"Перевод\s*(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})")
    return {
        "datetime_visible": dt_raw,
        "total": amount("Итого"),
        "amount": amount("Сумма"),
        "operation_id": grab(r"ID операции\s*([A-Z0-9]+)"),
        "phone": grab(r"(\+7 \(\d{3}\) \d{3}-\d{2}-\d{2})"),
        "recipient_bank": grab(r"Банк получателя\s*(.+?)(?:Отправитель|ID операции|По вопросам)"),
        "logo_in_text": bool(re.match(r"\s*ozon\s*банк", text, re.IGNORECASE)),
        "has_ruble": "₽" in text,
    }


def valid_operation_id(op_id):
    if op_id is None:
        return True, "ID отсутствует (короткая форма чека — допустимо)"
    if len(op_id) != 32:
        return False, f"длина {len(op_id)} != 32"
    bad = [i for i, c in enumerate(op_id)
           if c.isalpha() and i not in (0, 17)]
    if bad:
        return False, f"буквы на недопустимых позициях {bad} (норма: только 0 и 17)"
    if not op_id[0].isalpha() or not op_id[17].isalpha():
        return False, "на позициях 0/17 должны стоять буквы"
    return True, "формат корректен"


def timestamp_consistency(doc, fields):
    created = doc.info("CreationDate")
    notes = []
    if not created:
        return ["CreationDate отсутствует"], None
    m = re.match(r"D:(\d{14})", created)
    if not m:
        return ["CreationDate в нераспознанном формате"], None
    utc = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    msk = utc + dt.timedelta(hours=MSK_OFFSET_HOURS)

    if utc.minute == 0 and utc.second == 0:
        notes.append(f"подозрительно ровный таймстамп создания {utc:%H:%M:%S} UTC")

    if fields.get("datetime_visible"):
        try:
            vis = dt.datetime.strptime(fields["datetime_visible"], "%d.%m.%Y %H:%M")
            if abs((msk.replace(second=0) - vis).total_seconds()) > 120:
                notes.append(
                    f"видимое время {vis:%d.%m %H:%M} расходится с CreationDate "
                    f"{msk:%d.%m %H:%M} MSK"
                )
        except ValueError:
            pass
    return notes, created


class DedupStore:
    def __init__(self, path="seen_receipts.json"):
        self.path = path
        self.db = {"file_hashes": {}, "operation_ids": {}, "semantic_keys": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.db.update(json.load(fh))
            except (json.JSONDecodeError, OSError):
                pass

    @staticmethod
    def _file_hash(pdf_path):
        h = hashlib.sha256()
        with open(pdf_path, "rb") as fh:
            h.update(fh.read())
        return h.hexdigest()

    @staticmethod
    def _semantic_key(fields):
        return "|".join(str(fields.get(k)) for k in
                        ("phone", "total", "datetime_visible"))

    def check(self, pdf_path, fields):
        hits = []
        fh = self._file_hash(pdf_path)
        if fh in self.db["file_hashes"]:
            hits.append(f"идентичный файл уже встречался: {self.db['file_hashes'][fh]}")
        op = fields.get("operation_id")
        if op and op in self.db["operation_ids"]:
            hits.append(f"ID операции уже использован: {self.db['operation_ids'][op]}")
        key = self._semantic_key(fields)
        if key in self.db["semantic_keys"]:
            hits.append(f"связка телефон+сумма+время повторяется: {self.db['semantic_keys'][key]}")
        return hits

    def register(self, pdf_path, fields, label=None):
        label = label or os.path.basename(pdf_path)
        self.db["file_hashes"][self._file_hash(pdf_path)] = label
        op = fields.get("operation_id")
        if op:
            self.db["operation_ids"][op] = label
        self.db["semantic_keys"][self._semantic_key(fields)] = label
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.db, fh, ensure_ascii=False, indent=2)


class OzonReceiptChecker:

    def __init__(self, pdf_path, dedup=None):
        self.path = pdf_path
        self.doc = PdfDocument(pdf_path)
        self.text = self.doc.text()
        self.fields = extract_fields(self.text)
        self.dedup = dedup
        self.hard_fails = []
        self.soft_flags = []

    def _check_markers(self):
        low = self.text.lower()
        for marker in REQUIRED_MARKERS:
            if marker not in low:
                self.hard_fails.append(f"нет обязательного маркера «{marker}»")

    def _check_pipeline(self):
        fonts = self.doc.fonts()
        bold = fonts.get("GTEestiProDisplay-Bold")
        if not bold:
            self.hard_fails.append("встроенный Bold-шрифт Ozon не найден")
        elif bold["hash"] != GENUINE_BOLD_FONT_HASH:
            self.hard_fails.append(
                f"чужой хэш Bold-шрифта {bold['hash']} (эталон {GENUINE_BOLD_FONT_HASH}) "
                f"— документ собран не пайплайном Ozon"
            )

        reg = fonts.get("GTEestiProDisplay-Regular")
        if reg and bold and reg["prefix"] > bold["prefix"]:
            self.hard_fails.append(
                f"переставлен порядок встраивания шрифтов "
                f"(Regular={reg['prefix']}, Bold={bold['prefix']}) — чужой генератор"
            )

        img_hashes = {im["hash"] for im in self.doc.images()}
        extra = img_hashes - GENUINE_IMAGE_HASHES
        missing = GENUINE_IMAGE_HASHES - img_hashes
        if extra:
            self.hard_fails.append(f"посторонние изображения {sorted(extra)} (внедрён чужой элемент)")
        if missing:
            self.hard_fails.append(f"нет эталонного ассета печати {sorted(missing)}")

        if self.fields["logo_in_text"]:
            self.hard_fails.append("логотип «ozon банк» в текстовом слое — чужой рендер")

        if self.doc.page_width and self.doc.page_width != GENUINE_PAGE_WIDTH:
            self.soft_flags.append(
                f"ширина страницы {self.doc.page_width} != {GENUINE_PAGE_WIDTH}"
            )

        if self.doc.info("Producer") != GENUINE_PRODUCER:
            self.hard_fails.append(f"Producer={self.doc.info('Producer')!r} != {GENUINE_PRODUCER!r}")
        if self.doc.info("Creator") != GENUINE_CREATOR:
            self.hard_fails.append(f"Creator={self.doc.info('Creator')!r} != {GENUINE_CREATOR!r}")

        if self.doc.has_incremental_update:
            self.hard_fails.append("обнаружены инкрементальные правки PDF (документ редактировали)")

    def _check_operation_id(self):
        ok, why = valid_operation_id(self.fields["operation_id"])
        if not ok:
            self.hard_fails.append(f"невалидный формат ID операции: {why}")

    def _check_amounts(self):
        total, amount = self.fields["total"], self.fields["amount"]
        if total is not None and amount is not None and total != amount:
            self.hard_fails.append(f"«Итого» {total} != «Сумма» {amount}")

    def _check_timestamp(self):
        notes, _ = timestamp_consistency(self.doc, self.fields)
        self.soft_flags.extend(notes)

    def _check_dedup(self):
        if self.dedup is None:
            return
        for hit in self.dedup.check(self.path, self.fields):
            self.hard_fails.append(f"дубликат: {hit}")

    def analyze(self):
        if not self.text:
            return self._verdict("REJECTED", ["не удалось извлечь текстовый слой"])

        self._check_markers()
        self._check_pipeline()
        self._check_operation_id()
        self._check_amounts()
        self._check_timestamp()
        self._check_dedup()

        if self.hard_fails:
            status = "REJECTED"
        elif self.soft_flags:
            status = "SUSPICIOUS"
        else:
            status = "CLEAN"
        return self._verdict(status, None)

    def _verdict(self, status, override_reasons):
        advice = {
            "REJECTED": "Документ подделан или повторно использован — отклонить.",
            "SUSPICIOUS": "Структура чистая, но есть флаги. Требуется сверка ID операции "
                          "у банка/получателя перед зачислением.",
            "CLEAN": "Аномалий в файле нет. ВНИМАНИЕ: это не доказывает поступление денег — "
                     "обязательна внешняя сверка (ID операции / выписка получателя).",
        }[status]
        return {
            "status": status,
            "advice": advice,
            "hard_fails": override_reasons if override_reasons is not None else self.hard_fails,
            "soft_flags": self.soft_flags,
            "fields": self.fields,
        }


def check_receipt(pdf_path, dedup=None):
    return OzonReceiptChecker(pdf_path, dedup=dedup).analyze()


VERDICT_RU = {
    "REJECTED": "ФЕЙК",
    "SUSPICIOUS": "ПОДОЗРИТЕЛЬНО",
    "CLEAN": "ЧИСТО (нужна сверка)",
}


def scan_folder(folder, dedup_path=None):
    import glob

    paths = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    if dedup_path and os.path.exists(dedup_path):
        os.remove(dedup_path)
    dedup = DedupStore(path=dedup_path) if dedup_path else None

    results = []
    for path in paths:
        try:
            res = check_receipt(path, dedup=dedup)
        except Exception as exc:
            res = {
                "status": "REJECTED",
                "advice": "Файл не удалось разобрать как PDF.",
                "hard_fails": [f"ошибка разбора: {exc}"],
                "soft_flags": [],
                "fields": {},
            }
        if dedup:
            dedup.register(path, res.get("fields", {}))
        results.append((os.path.basename(path), res))

    if dedup_path and os.path.exists(dedup_path):
        os.remove(dedup_path)
    return results


def build_report(results, folder):
    valid = [name for name, res in results if res["status"] != "REJECTED"]
    fake = [name for name, res in results if res["status"] == "REJECTED"]

    def table(names, badge):
        if not names:
            return ["_нет_", ""]
        col = max([len("Файл")] + [len(n) for n in names])
        rows = [
            f"| {'Файл'.ljust(col)} | Статус  |",
            f"| {'-' * col} | ------- |",
        ]
        for n in names:
            rows.append(f"| {n.ljust(col)} | {badge} |")
        rows.append("")
        return rows

    L = []
    L.append("## Валидные документы")
    L.append("")
    L.extend(table(valid, "✅ Валид"))
    L.append("## Фейковые документы")
    L.append("")
    L.extend(table(fake, "❌ Фейк "))
    return "\n".join(L).rstrip() + "\n"


if __name__ == "__main__":
    import sys

    base = os.path.dirname(os.path.abspath(__file__))
    folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, "чеки")

    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
        print(f"Создана папка: {folder}")
        print("Положите в неё PDF-чеки и запустите снова: python3 main.py")
        raise SystemExit

    results = scan_folder(folder, dedup_path=os.path.join(base, "_run_dedup.json"))
    if not results:
        print(f"В папке {folder} нет PDF-файлов.")
        raise SystemExit

    report = build_report(results, folder)
    out_path = os.path.join(base, "отчет.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    for name, res in results:
        print(f"{VERDICT_RU.get(res['status'], res['status']):22} {name}")
    print(f"\nОтчёт сохранён")
