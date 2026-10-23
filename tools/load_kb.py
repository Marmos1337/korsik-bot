"""Loads files into the knowledge base.

python -m tools.load_kb PATH [PATH ...] [--tags "GTP, офис"] [--title "Название"]
python -m tools.load_kb kb-demo --demo     demo files: the tag is the end of the name, food-GTP.md -> GTP

Each file is copied to data/kb/ first. Loading a file with the same name again replaces the document.
"""

import argparse
import asyncio
import shutil
from pathlib import Path

from app import db, knowledge, llm
from app.config import cfg

SUPPORTED = {".pdf", ".docx", ".pptx", ".txt", ".md"}


def files_in(paths: list[str]) -> list[Path]:
    files = []
    for name in paths:
        path = Path(name)
        if path.is_dir():
            files += sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED)
        else:
            files.append(path)
    return files


def title_for(path: Path) -> str:
    if path.suffix.lower() == ".md":
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return path.stem


async def load(files: list[Path], tags: list[str], title: str | None, demo: bool) -> None:
    db.init_db()
    if await llm.check():
        print("Ollama отвечает, векторы посчитаются сразу")
    else:
        print("Ollama недоступна: документы будут искаться по словам, векторы досчитаются позже")

    kb_dir = Path(cfg.DATA_DIR) / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        copy = kb_dir / path.name
        if path.resolve() != copy.resolve():
            shutil.copy2(path, copy)
        file_tags = [path.stem.rsplit("-", 1)[-1]] if demo else tags
        try:
            doc_id = await knowledge.add_document(
                copy, title or title_for(path), file_tags, kind="demo" if demo else "file"
            )
        except ValueError as e:
            print(f"{path.name}: пропущен, {e}")
            continue
        chunks = db.query_one("SELECT COUNT(*) AS n FROM chunks WHERE document_id = ?", (doc_id,))["n"]
        print(f"{path.name}: документ {doc_id}, кусков {chunks}, теги: {', '.join(file_tags) or 'для всех'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузить документы в базу знаний")
    parser.add_argument("paths", nargs="+", help="файлы или папки")
    parser.add_argument("--tags", default="", help="через запятую; без тегов документ видят все")
    parser.add_argument("--title", help="только для одного файла; по умолчанию имя файла или заголовок md")
    parser.add_argument("--demo", action="store_true", help="демо-файлы, тег берётся из конца имени")
    args = parser.parse_args()

    files = files_in(args.paths)
    if args.title and len(files) > 1:
        parser.error("--title можно задать только для одного файла")
    asyncio.run(load(files, db.split_tags(args.tags), args.title, args.demo))


if __name__ == "__main__":
    main()
