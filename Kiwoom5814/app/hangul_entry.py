"""Optional Hangul composition for alias entries on Linux."""
import ctypes
import sys
from tkinter import BooleanVar, ttk


def offer_hangul(entry, parent, **placement):
    if not sys.platform.startswith('linux'):
        return
    for name in ('libhangul.so.1', 'libhangul.so.0'):
        try:
            library = ctypes.CDLL(name)
            input_method = HangulInput(library, entry)
            break
        except (OSError, AttributeError, ValueError):
            continue
    else:
        return
    enabled = BooleanVar(master=entry)
    switch = ttk.Checkbutton(parent, text='한글 입력', variable=enabled,
                             command=lambda: input_method.toggle(enabled.get()))
    switch.grid(**placement)


def read_ucs4(chars):
    result = []
    index = 0
    while chars[index]:
        result.append(chr(chars[index]))
        index += 1
    return ''.join(result)


class HangulInput:
    def __init__(self, library, entry):
        self.entry = entry
        pointer = ctypes.c_void_p
        characters = ctypes.POINTER(ctypes.c_uint32)
        signatures = (
            ('new', [ctypes.c_char_p], pointer),
            ('delete', [pointer], None),
            ('process', [pointer, ctypes.c_int], ctypes.c_bool),
            ('backspace', [pointer], ctypes.c_bool),
            ('get_commit_string', [pointer], characters),
            ('get_preedit_string', [pointer], characters),
            ('flush', [pointer], characters),
        )
        for name, args, result in signatures:
            function = getattr(library, 'hangul_ic_' + name)
            function.argtypes, function.restype = args, result
            setattr(self, '_' + name, function)
        self.context = self._new(b'2')
        if not self.context:
            raise ValueError('Hangul context unavailable')
        self.enabled, self.pending = False, 0
        entry.bind('<KeyPress>', self.key, add='+')
        entry.bind('<FocusOut>', self.finish, add='+')
        entry.bind('<Button-1>', self.finish, add='+')
        entry.bind('<Destroy>', self.destroy, add='+')

    def toggle(self, enabled):
        self.finish()
        self.enabled = enabled
        self.entry.focus_set()

    def destroy(self, _event):
        if self.context:
            self._delete(self.context)
            self.context = None

    def update_text(self, committed, composing):
        entry = self.entry
        cursor = entry.index('insert')
        if self.pending:
            cursor -= self.pending
            entry.delete(cursor, cursor + self.pending)
        elif entry.selection_present():
            cursor = entry.index('sel.first')
            entry.delete('sel.first', 'sel.last')
        entry.insert(cursor, committed + composing)
        entry.icursor(cursor + len(committed + composing))
        self.pending = len(composing)

    def finish(self, _event=None):
        if self.pending:
            self.update_text(read_ucs4(self._flush(self.context)), '')

    def key(self, event):
        if not self.enabled:
            return
        # Preserve Ctrl, Alt and Super shortcuts, and accept Unicode from the OS IME.
        if not event.state & (0x4 | 0x8 | 0x40):
            if event.keysym == 'BackSpace' and self.pending and self._backspace(self.context):
                self.update_text('', read_ucs4(self._get_preedit_string(self.context)))
                return 'break'
            char = event.char
            if len(char) == 1 and char.isascii() and char.isalpha() and self._process(self.context, ord(char)):
                commit = read_ucs4(self._get_commit_string(self.context))
                preedit = read_ucs4(self._get_preedit_string(self.context))
                self.update_text(commit, preedit)
                return 'break'
        self.finish()
