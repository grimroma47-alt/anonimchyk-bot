# Запускач бота: склеює частини і запускає їх як один файл.
# Бот розбитий на частини, бо при копіюванні з телефона вставляється
# не більше ~100 000 символів, і довший файл мовчки обрізався.
# Щоб додати нову частину — створи наступний partN.py і допиши його в PARTS.
import pathlib

PARTS = ["part1.py", "part2.py", "part3.py", "part4.py"]

_here = pathlib.Path(__file__).resolve().parent
_code = ""
for _name in PARTS:
    _path = _here / _name
    if not _path.exists():
        raise SystemExit(f"❌ Не знайдено файл {_name} поруч із запускачем")
    _text = _path.read_text(encoding="utf-8")
    _marker = f"# === КІНЕЦЬ {_name} ==="
    if _marker not in _text[-300:]:
        raise SystemExit(
            f"❌ Файл {_name} обрізаний або вставлений не повністю! "
            f"Останній рядок у ньому має бути: {_marker}"
        )
    _code += _text.rstrip("\n") + "\n\n"

exec(compile(_code, "bot_full.py", "exec"))
