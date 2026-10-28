"""Разбор EPUB без внешних библиотек: обложка, автор, главы.

EPUB — это zip: META-INF/container.xml → *.opf (метаданные и порядок глав)
→ XHTML-файлы. Главы режем по <h1>, как в оглавлении книги.
"""
from __future__ import annotations

import html as htmllib
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

DC = "{http://purl.org/dc/elements/1.1/}"
OPF = "{http://www.idpf.org/2007/opf}"
CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"

H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S | re.I)
H2 = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")


class TextExtractor(HTMLParser):
    """HTML → простой текст: абзацы с новой строки, без тегов и скриптов."""

    BLOCKS = {
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr",
        "blockquote", "section", "article", "header", "footer", "figure",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (line.strip() for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(fragment: str) -> str:
    parser = TextExtractor()
    parser.feed(fragment)
    parser.close()
    return parser.text()


def clean_title(inner: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", inner, flags=re.I)
    text = htmllib.unescape(TAG.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def split_chapters(document: str) -> list[dict]:
    """Режет XHTML на главы по <h1>. Если h1 нет — по <h2>, иначе одна глава."""
    for pattern in (H1, H2):
        marks = list(pattern.finditer(document))
        if not marks:
            continue
        chapters = []
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(document)
            body = document[mark.end():end]
            chapters.append({
                "title": clean_title(mark.group(1)),
                "text": html_to_text(body),
            })
        return chapters
    return []


def _dirname(path: str) -> str:
    return posixpath.dirname(path)


def _read(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        return z.read(name)
    except KeyError:
        from urllib.parse import unquote
        return z.read(unquote(name))


def _is_chapter(chapter: dict) -> bool:
    """Пустое — не глава. Глава с числовым названием — сноска из примечаний."""
    title = chapter["title"]
    if not chapter["text"]:
        return False
    return not re.fullmatch(r"\d+", title)


def read_epub(source: bytes | str) -> dict:
    """Возвращает title, author, lang, cover (bytes|None, ext), chapters."""
    if isinstance(source, (bytes, bytearray)):
        z = zipfile.ZipFile(io.BytesIO(source))
    else:
        z = zipfile.ZipFile(source)

    container = ET.fromstring(z.read("META-INF/container.xml"))
    rootfile = container.find(f".//{CONTAINER}rootfile")
    if rootfile is None:
        raise ValueError("В EPUB нет META-INF/container.xml — файл повреждён.")
    opf_path = rootfile.get("full-path")
    if not opf_path:
        raise ValueError("В container.xml не указан путь к OPF.")
    opf = ET.fromstring(_read(z, opf_path))
    base = _dirname(opf_path)

    title = re.sub(r"\s+", " ", opf.findtext(f".//{DC}title") or "").strip() or "Без названия"
    author = re.sub(r"\s+", " ", opf.findtext(f".//{DC}creator") or "").strip()
    lang = (opf.findtext(f".//{DC}language") or "").strip()

    items = {}
    for item in opf.findall(f".//{OPF}item"):
        items[item.get("id")] = item

    # обложка: <meta name="cover" content="id"> или properties="cover-image"
    cover_id = None
    for meta in opf.findall(f".//{OPF}meta"):
        if meta.get("name") == "cover":
            cover_id = meta.get("content")
    cover_item = items.get(cover_id) if cover_id else None
    if cover_item is None:
        for item in items.values():
            props = item.get("properties") or ""
            if "cover-image" in props:
                cover_item = item
                break
    cover_bytes = None
    cover_ext = None
    if cover_item is not None and cover_item.get("href"):
        href = posixpath.normpath(posixpath.join(base, cover_item.get("href")))
        try:
            cover_bytes = _read(z, href)
            media = cover_item.get("media-type") or ""
            cover_ext = ".jpg" if "jpeg" in media else (".png" if "png" in media else
                        posixpath.splitext(href)[1].lower() or ".jpg")
            # Обложкой бывает и XHTML-страница с картинкой: её нельзя
            # показывать как картинку, поэтому берём только изображения.
            if not media.startswith("image/") and cover_ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                cover_bytes = None
                cover_ext = None
        except KeyError:
            cover_bytes = None

    spine = opf.find(f".//{OPF}spine")
    order = []
    if spine is not None:
        for ref in spine.findall(f"{OPF}itemref"):
            item = items.get(ref.get("idref"))
            if item is None:
                continue
            media = item.get("media-type") or ""
            if media not in ("application/xhtml+xml", "text/html"):
                continue
            href = (item.get("href") or "").split("#")[0]
            if href:
                order.append(posixpath.normpath(posixpath.join(base, href)))

    chapters = []
    for href in order:
        try:
            document = _read(z, href).decode("utf-8", "replace")
        except KeyError:
            continue
        for chapter in split_chapters(document):
            if _is_chapter(chapter):
                chapters.append(chapter)

    if not chapters:
        raise ValueError("В книге не нашлось глав — не вижу текста в EPUB.")

    return {
        "title": title,
        "author": author,
        "lang": lang,
        "cover": cover_bytes,
        "cover_ext": cover_ext,
        "chapters": chapters,
    }
