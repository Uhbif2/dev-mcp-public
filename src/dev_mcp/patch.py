"""Пакетная правка в формате apply_patch (Codex / V4A) — разбор и применение к тексту (27.09.2026).

Зачем. Одна правка = один вызов edit_file: задача на пять файлов стоила агенту десяток вызовов,
каждый со своим конвертом и шансом закончить ход посередине. Формат взят у Codex не случайно:
модели ChatGPT обучены писать именно его, придумывать свой диалект — значит учить агента
заново.

    *** Begin Patch
    *** Update File: src/app.py
    @@ def main():
     context
    -old line
    +new line
    *** Add File: src/new.py
    +print("hi")
    *** End Patch

Здесь только чистые функции над текстом: границы, страж флота и запись — в server.py, и там
же правило «всё или ничего». Удаление и перенос файлов не поддерживаются намеренно: у dev-mcp
нет удаления, и пакетная правка не должна стать обходом этого.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class PatchError(ValueError):
    pass


@dataclass
class Chunk:
    anchor: str = ''
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    context: int = 0
    at_eof: bool = False


@dataclass
class FileOp:
    kind: str  # 'update' | 'add'
    path: str
    chunks: list[Chunk] = field(default_factory=list)
    content: str = ''


_BEGIN, _END = '*** Begin Patch', '*** End Patch'
_UPDATE, _ADD = '*** Update File: ', '*** Add File: '
_DELETE, _MOVE, _EOF = '*** Delete File: ', '*** Move to: ', '*** End of File'


def parse(patch: str) -> list[FileOp]:
    lines = patch.replace('\r\n', '\n').strip('\n').split('\n')
    # Обёртки вокруг патча (```, heredoc) агенты добавляют часто — снимаем, а не отказываем.
    while lines and lines[0].strip() != _BEGIN and not lines[0].startswith('***'):
        lines.pop(0)
    while lines and lines[-1].strip() != _END and not lines[-1].startswith(('+', '-', ' ', '@@', '***')):
        lines.pop()
    if not lines or lines[0].strip() != _BEGIN:
        raise PatchError(f'патч должен начинаться строкой «{_BEGIN}»')
    if lines[-1].strip() != _END:
        raise PatchError(f'патч должен кончаться строкой «{_END}»')
    ops: list[FileOp] = []
    body = lines[1:-1]
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith(_ADD):
            op = FileOp('add', line[len(_ADD):].strip())
            i += 1
            content: list[str] = []
            while i < len(body) and not body[i].startswith('*** '):
                if not body[i].startswith('+'):
                    raise PatchError(f'{op.path}: в «Add File» каждая строка начинается с «+», а не: {body[i][:60]!r}')
                content.append(body[i][1:])
                i += 1
            op.content = '\n'.join(content) + ('\n' if content else '')
            ops.append(op)
        elif line.startswith(_UPDATE):
            op = FileOp('update', line[len(_UPDATE):].strip())
            i += 1
            if i < len(body) and body[i].startswith(_MOVE):
                raise PatchError(f'{op.path}: перенос файлов (Move to) не поддерживается')
            chunk: Chunk | None = None
            while i < len(body) and not body[i].startswith(('*** Update', '*** Add', '*** Delete')):
                cur = body[i]
                if cur.startswith('@@'):
                    chunk = Chunk(anchor=cur[2:].strip())
                    op.chunks.append(chunk)
                elif cur.strip() == _EOF:
                    if chunk is None:
                        raise PatchError(f'{op.path}: «{_EOF}» до первого фрагмента')
                    chunk.at_eof = True
                else:
                    if chunk is None:
                        chunk = Chunk()
                        op.chunks.append(chunk)
                    mark, text = (cur[0], cur[1:]) if cur else (' ', '')
                    if mark == ' ':
                        chunk.old.append(text)
                        chunk.new.append(text)
                        chunk.context += 1
                    elif mark == '-':
                        chunk.old.append(text)
                    elif mark == '+':
                        chunk.new.append(text)
                    else:
                        raise PatchError(
                            f'{op.path}: строка фрагмента должна начинаться с « », «-» или «+», а не: {cur[:60]!r}')
                i += 1
            if not op.chunks:
                raise PatchError(f'{op.path}: «Update File» без единого фрагмента')
            ops.append(op)
        elif line.startswith(_DELETE):
            raise PatchError('удаление файлов (Delete File) через dev-mcp не поддерживается')
        elif not line.strip():
            i += 1
        else:
            raise PatchError(f'ожидался «*** Update File:» или «*** Add File:», а не: {line[:80]!r}')
    if not ops:
        raise PatchError('в патче нет ни одного файла')
    paths = [op.path for op in ops]
    dup = {p for p in paths if paths.count(p) > 1}
    if dup:
        raise PatchError(f'один файл в патче дважды: {", ".join(sorted(dup))} — объедини фрагменты')
    return ops


def _find(lines: list[str], needle: list[str], start: int, at_eof: bool) -> int:
    """Позиция needle в lines не раньше start: точно, затем без хвостовых пробелов, затем без
    отступов (агент часто теряет пробелы в конце строк или путает табы). -1 — не нашли."""
    if not needle:
        return len(lines) if at_eof else start
    for norm in (lambda s: s, str.rstrip, str.strip):
        want = [norm(x) for x in needle]
        have = [norm(x) for x in lines]
        rng = [len(lines) - len(needle)] if at_eof else range(start, len(lines) - len(needle) + 1)
        for pos in rng:
            if pos >= start and have[pos:pos + len(needle)] == want:
                return pos
    return -1


def apply(text: str, op: FileOp) -> tuple[str, int, int]:
    """Применить фрагменты update к тексту (переводы строк — '\\n'). → (текст, +строк, −строк)."""
    lines = text.split('\n')
    trailing = bool(lines) and lines[-1] == ''
    if trailing:
        lines.pop()
    cursor = 0
    added = removed = 0
    for n, chunk in enumerate(op.chunks, 1):
        start = cursor
        if chunk.anchor:
            hit = _find(lines, [chunk.anchor], cursor, False)
            if hit < 0:
                hit = next((k for k in range(cursor, len(lines)) if chunk.anchor.strip() in lines[k]), -1)
            if hit < 0:
                raise PatchError(f'{op.path}: фрагмент {n}: не найдена строка-якорь «@@ {chunk.anchor[:80]}»')
            start = hit + (0 if chunk.old and lines[hit].strip() == chunk.old[0].strip() else 1)
        pos = _find(lines, chunk.old, start, chunk.at_eof)
        if pos < 0:
            shown = '\n'.join(chunk.old[:6])
            raise PatchError(
                f'{op.path}: фрагмент {n}: строки контекста/удаления не найдены (после строки {start + 1}):\n'
                f'{shown}\nПеречитай файл read_file и повтори патч по текущему тексту.')
        lines[pos:pos + len(chunk.old)] = chunk.new
        cursor = pos + len(chunk.new)
        removed += len(chunk.old) - chunk.context
        added += len(chunk.new) - chunk.context
    return '\n'.join(lines) + ('\n' if trailing else ''), added, removed
