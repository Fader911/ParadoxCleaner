"""Paradox Cleaner — локальная утилита очистки Windows. Python 3.10+, без пакетов."""
import csv
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


@dataclass(frozen=True)
class Game:
    title: str
    mark: str
    subtitle: str
    exe: str
    app_id: str


GAMES = (
    Game('Victoria 3', 'V III', 'ВИКТОРИАНСКАЯ ЭПОХА', 'victoria3.exe', '529340'),
    Game('Hearts of Iron IV', 'HOI IV', 'МИРОВАЯ ВОЙНА', 'hoi4.exe', '394360'),
    Game('Crusader Kings III', 'CK III', 'ДИНАСТИИ И КОРОНЫ', 'ck3.exe', '1158310'),
    Game('Europa Universalis IV', 'EU IV', 'ЭПОХА ИМПЕРИЙ', 'eu4.exe', '236850'),
    Game('Stellaris', '✦', 'ЗА ПРЕДЕЛАМИ ЗВЁЗД', 'stellaris.exe', '281990'),
)
CATEGORIES = {
    'install': ('Файлы игры', 'Полная папка установки для переустановки.'),
    'settings': ('Настройки и кеш', 'Конфигурации, логи, шейдеры, временные файлы.'),
    'mods': ('Моды и данные лаунчера', 'Локальные моды, списки модов, кеш и база лаунчера.'),
    'workshop': ('Steam Workshop', 'Скачанные моды только выбранной игры.'),
    'saves': ('Сохранения', 'Сейвы; включай только если они больше не нужны.'),
}
MOD_NAMES = {'mod', 'mods', 'mods_registry.json', 'dlc_load.json', 'content_load.json',
             'game_data.json', '.launcher-cache', 'launcher-v2.sqlite', 'launcher-v2.sqlite-wal',
             'launcher-v2.sqlite-shm', 'newsfeed', 'playsets.json'}
SAVE_NAMES = {'save games', 'savegames', 'saves', 'save_games', 'savegame'}
SAVE_EXT = {'.v3', '.hoi4', '.ck3', '.eu4', '.sav', '.ck2'}
BACKUP_PREFIX = '.ParadoxCleaner_Backup_'
BG, PANEL, INSET = '#151c22', '#202b32', '#192329'
GOLD, MUTED, INK = '#c8a76c', '#9caaa9', '#e8dfcb'
LINE, RED, GREEN = '#49504e', '#b86758', '#78ac99'


def pathkey(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def unique(paths):
    return list({pathkey(p): Path(p) for p in paths}.values())


def within(path, parent):
    try:
        return os.path.commonpath([pathkey(path), pathkey(parent)]) == pathkey(parent)
    except ValueError:
        return False


def reparse(path):
    """Не следуем по символическим ссылкам и NTFS junction."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def safe_existing(path, allow_root=False):
    if not path.is_absolute() or (path == path.parent and not allow_root) or not path.exists():
        return False
    try:
        return not any(reparse(p) for p in (path, *path.parents) if p.exists())
    except OSError:
        return False


def documents_dirs():
    paths = [Path.home() / 'Documents']
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                value, _ = winreg.QueryValueEx(key, 'Personal')
                paths.insert(0, Path(os.path.expandvars(value)))
        except OSError:
            pass
    for var in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.environ.get(var):
            paths.append(Path(os.environ[var]) / 'Documents')
    return unique(paths)


def steam_libraries():
    paths = []
    if os.name == 'nt':
        import winreg
        for hive, key, field in [(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                                 (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Valve\Steam', 'InstallPath')]:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    paths.append(Path(winreg.QueryValueEx(handle, field)[0]))
            except OSError:
                pass
    if os.environ.get('ProgramFiles(x86)'):
        paths.append(Path(os.environ['ProgramFiles(x86)']) / 'Steam')
    for root in list(paths):
        try:
            vdf = (root / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
            paths.extend(Path(x.replace('\\\\', '\\')) for x in re.findall(r'"path"\s*"([^"]+)"', vdf))
        except OSError:
            pass
    return unique(p for p in paths if p.is_absolute() and p.is_dir())


def validated_install(path, game):
    if not safe_existing(path) or not path.is_dir():
        return False
    # Require both a game-specific executable and a characteristic game-data folder.
    return (any((path / rel / game.exe).is_file() for rel in ('', 'binaries'))
            and any((path / rel).is_dir() for rel in ('game', 'common', 'localisation', 'map')))


def classify(path):
    name = path.name.lower()
    if name in SAVE_NAMES or path.suffix.lower() in SAVE_EXT:
        return 'saves'
    if name in MOD_NAMES or name.startswith('launcher-v2.sqlite-'):
        return 'mods'
    if name in {'screenshots', 'screen shots', 'screenshots_backup'} or name.startswith(BACKUP_PREFIX.lower()):
        return None
    return 'settings'


@dataclass(frozen=True)
class Candidate:
    category: str
    path: Path
    backup_parent: Path


def discover(game, selection, install=None, docs=None, libraries=None, appdata=None):
    """Только чтение. Все пути вычисляются из выбранного профиля игры."""
    docs = documents_dirs() if docs is None else docs
    libraries = steam_libraries() if libraries is None else libraries
    appdata = [Path(os.environ[k]) for k in ('APPDATA', 'LOCALAPPDATA') if os.environ.get(k)] if appdata is None else appdata
    found, warnings = [], []

    def add(cat, path, parent):
        if cat in selection and path.exists():
            if safe_existing(path) and safe_existing(parent, allow_root=True) and not within(parent, path):
                found.append(Candidate(cat, path, parent))
            else:
                warnings.append(f'Пропущен небезопасный путь: {path}')

    manual = Path(install) if install else None
    if manual:
        if validated_install(manual, game):
            add('install', manual, manual.parent)
        elif 'install' in selection:
            warnings.append(f'Папка установки не подтверждена: {manual}. Нужны {game.exe} и данные игры.')
    else:
        for lib in libraries:
            root = lib / 'steamapps' / 'common' / game.title
            # Official Steam library is game-specific even after Steam removed the executable.
            if root.is_dir():
                add('install', root, root.parent)

    for base in unique([*docs, *appdata]):
        root = base / 'Paradox Interactive' / game.title
        if not root.exists():
            continue
        if not safe_existing(root) or not root.is_dir():
            warnings.append(f'Пропущена папка с перенаправлением или ссылкой: {root}')
            continue
        try:
            for child in root.iterdir():
                cat = classify(child)
                if cat:
                    add(cat, child, root.parent)
        except OSError as e:
            warnings.append(f'{root}: {e}')

    for lib in libraries:
        workshop = lib / 'steamapps' / 'workshop' / 'content' / game.app_id
        add('workshop', workshop, workshop.parent)
        manifest = lib / 'steamapps' / 'workshop' / f'appworkshop_{game.app_id}.acf'
        add('workshop', manifest, manifest.parent)

    # Remove duplicates and nested targets (e.g. overlapping install and data folders).
    clean = []
    for item in sorted({pathkey(x.path): x for x in found}.values(), key=lambda x: len(x.path.parts)):
        if not any(within(item.path, x.path) for x in clean):
            clean.append(item)
    return clean, warnings


def journal_dir():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ParadoxCleaner' / 'history'


def write_journal(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def quarantine(game, candidates, history=None):
    """Перемещение, а не удаление. Оригиналы остаются на том же диске."""
    session = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    log = (journal_dir() if history is None else history) / f'{session}.json'
    records = []
    for number, item in enumerate(candidates):
        backup = item.backup_parent / (BACKUP_PREFIX + session) / f'{number:03d}' / item.path.name
        records.append({'original': str(item.path), 'backup': str(backup), 'category': item.category})
    data = {'game': game.title, 'session': session, 'entries': records}
    write_journal(log, data)  # journal created BEFORE any moves
    moved, failed = [], []
    for item, record in zip(candidates, records):
        dest = Path(record['backup'])
        try:
            if not safe_existing(item.path) or not safe_existing(item.backup_parent, allow_root=True) or dest.exists():
                raise OSError('Путь изменился после сканирования или архив уже существует.')
            dest.parent.mkdir(parents=True, exist_ok=False)
            if not safe_existing(dest.parent):
                raise OSError('Небезопасный архивный путь.')
            item.path.rename(dest)
            moved.append(record)
        except OSError as e:
            failed.append(f'{item.path}: {e}')
    return log, moved, failed


def restore(log):
    data = json.loads(log.read_text(encoding='utf-8'))
    restored, failed = [], []
    session = data['session']
    for row in reversed(data['entries']):
        source, backup = Path(row['original']), Path(row['backup'])
        if not backup.exists():
            continue
        try:
            if (not source.is_absolute() or source == source.parent or source.exists()
                    or not safe_existing(backup) or backup.name != source.name
                    or backup.parent.parent.name != BACKUP_PREFIX + session
                    or pathkey(backup.parent.parent.parent) not in {pathkey(source.parent), pathkey(source.parent.parent)}
                    or not safe_existing(source.parent, allow_root=True)):
                raise OSError('Исходный путь занят, отсутствует или небезопасен. Нужна ручная проверка.')
            backup.rename(source)
            restored.append(str(source))
        except OSError as e:
            failed.append(f'{source}: {e}')
    return restored, failed


def running(game):
    if os.name != 'nt':
        return []
    try:
        result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                                text=True, errors='replace', timeout=8, creationflags=0x08000000)
        names = {r[0].lower() for r in csv.reader(result.stdout.splitlines()) if r}
        return sorted(names & {game.exe.lower(), 'paradox launcher.exe', 'paradoxlauncher.exe', 'dowser.exe', 'steam.exe'})
    except (OSError, subprocess.TimeoutExpired):
        return []


class Cleaner(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Paradox Cleaner  •  Checksum Recovery')
        self.geometry('1120x790')
        self.minsize(890, 690)
        self.configure(bg=BG)
        self.game = GAMES[0]
        self.plan = []
        self.paths = {}
        self.path = tk.StringVar(value=self.paths.get(self.game.title, ''))
        self.checked = {k: tk.BooleanVar(value=k != 'saves') for k in CATEGORIES}
        self.status = tk.StringVar(value='Готово к сканированию')
        self.style_ui()
        self.build()
        self.path.trace_add('write', lambda *_: self.invalidate())
        self.select_game(self.game)

    def style_ui(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('.', font=('Segoe UI', 10), background=PANEL, foreground=INK, bordercolor=LINE)
        s.configure('TButton', background='#303d42', foreground=INK, padding=(12, 8), relief='flat')
        s.map('TButton', background=[('active', '#455452'), ('disabled', INSET)], foreground=[('disabled', MUTED)])
        s.configure('Gold.TButton', background=GOLD, foreground=BG, font=('Segoe UI', 12, 'bold'), padding=(20, 12))
        s.map('Gold.TButton', background=[('active', '#e4bf7a'), ('disabled', '#5b5a50')])
        s.configure('TEntry', fieldbackground=INSET, foreground=INK, insertcolor=INK, padding=8)
        s.configure('TCheckbutton', background=PANEL, foreground=INK, font=('Segoe UI', 10, 'bold'))
        s.map('TCheckbutton', background=[('active', PANEL)], indicatorcolor=[('selected', GOLD)])
        s.configure('Treeview', background=INSET, fieldbackground=INSET, foreground=INK, rowheight=29, relief='flat')
        s.configure('Treeview.Heading', background='#35413f', foreground=GOLD, font=('Segoe UI', 9, 'bold'), padding=8)
        s.map('Treeview', background=[('selected', '#465549')], foreground=[('selected', INK)])

    def label(self, parent, text, size=10, color=INK, bold=False, bg=None, serif=False, **kw):
        return tk.Label(parent, text=text, font=('Georgia' if serif else 'Segoe UI', size, 'bold' if bold else 'normal'),
                        fg=color, bg=bg or parent.cget('bg'), **kw)

    def panel(self, parent, **kw):
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, **kw)

    def build(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill='x', padx=24, pady=(19, 10))
        self.label(header, '◆  PARADOX CLEANER', 23, GOLD, True, serif=True).pack(anchor='w')
        self.label(header, 'CHECKSUM RECOVERY  /  ИНСТРУМЕНТ ВОССТАНОВЛЕНИЯ ИГР', 9, MUTED).pack(anchor='w', pady=(3, 0))
        tk.Frame(self, bg=GOLD, height=1).pack(fill='x', padx=24, pady=(0, 12))
        tabs = tk.Frame(self, bg=BG)
        tabs.pack(fill='x', padx=24)
        self.cards = {}
        for i, game in enumerate(GAMES):
            tabs.columnconfigure(i, weight=1, uniform='game')
            frame = tk.Frame(tabs, bg=PANEL, highlightthickness=1, highlightbackground=LINE, cursor='hand2')
            frame.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 4, 0 if i == 4 else 4))
            mark = self.label(frame, game.mark, 22, GOLD, True, serif=True)
            mark.pack(pady=(10, 2))
            title = self.label(frame, game.title, 10, INK, True)
            title.pack(pady=(0, 3))
            sub = self.label(frame, game.subtitle, 8, MUTED)
            sub.pack(pady=(0, 10))
            for widget in (frame, mark, title, sub):
                widget.bind('<Button-1>', lambda _e, g=game: self.select_game(g))
            self.cards[game.title] = (frame, mark, title, sub)
        top = self.panel(self)
        top.pack(fill='x', padx=24, pady=(14, 10))
        self.game_label = self.label(top, '', 15, GOLD, True, serif=True)
        self.game_label.pack(anchor='w', padx=15, pady=(12, 1))
        self.label(top, 'Папка установленной игры (пустое поле — автопоиск Steam)', 9, MUTED).pack(anchor='w', padx=15, pady=(0, 7))
        row = tk.Frame(top, bg=PANEL)
        row.pack(fill='x', padx=15, pady=(0, 13))
        ttk.Entry(row, textvariable=self.path).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Обзор…', command=self.browse).pack(side='left', padx=(8, 0))
        ttk.Button(row, text='Автопоиск', command=self.auto).pack(side='left', padx=(8, 0))
        middle = tk.Frame(self, bg=BG)
        middle.pack(fill='both', expand=True, padx=24, pady=(0, 10))
        left = self.panel(middle, width=330)
        left.pack(side='left', fill='y', padx=(0, 10))
        left.pack_propagate(False)
        self.label(left, 'I.  ОБЛАСТЬ ОЧИСТКИ', 12, GOLD, True, serif=True).pack(anchor='w', padx=14, pady=(13, 7))
        for k, (name, desc) in CATEGORIES.items():
            ttk.Checkbutton(left, text=name, variable=self.checked[k], command=self.invalidate).pack(anchor='w', padx=13, pady=(6, 0))
            self.label(left, desc, 9, MUTED, wraplength=282, justify='left').pack(anchor='w', padx=33, pady=(1, 5))
        ttk.Button(left, text='Найти файлы  /  Сканировать', command=self.scan).pack(fill='x', padx=14, pady=12)
        right = self.panel(middle)
        right.pack(side='left', fill='both', expand=True)
        self.label(right, 'II.  РАЗВЕДДАННЫЕ', 12, GOLD, True, serif=True).pack(anchor='w', padx=14, pady=(13, 7))
        self.report = self.label(right, 'Выполни сканирование, чтобы увидеть точные пути.', 9, MUTED, wraplength=490, justify='left')
        self.report.pack(anchor='w', padx=14, pady=(0, 7))
        table = tk.Frame(right, bg=INSET)
        table.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self.tree = ttk.Treeview(table, columns=('type', 'path'), show='headings', selectmode='browse')
        self.tree.heading('type', text='КАТЕГОРИЯ')
        self.tree.heading('path', text='ПУТЬ')
        self.tree.column('type', width=155, minwidth=120, stretch=False)
        self.tree.column('path', width=500, minwidth=260)
        sb = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        hs = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hs.set)
        table.rowconfigure(0, weight=1); table.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky='nsew'); sb.grid(row=0, column=1, sticky='ns'); hs.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<Double-1>', self.show_path)
        footer = self.panel(self)
        footer.pack(fill='x', padx=24, pady=(0, 10))
        self.label(footer, '◆  Безопасный режим: файлы перемещаются в архив на том же диске.', 10, GREEN).pack(anchor='w', padx=14, pady=(9, 3))
        buttons = tk.Frame(footer, bg=PANEL)
        buttons.pack(fill='x', padx=14, pady=(5, 11))
        ttk.Button(buttons, text='История / Восстановить', command=self.history).pack(side='left')
        ttk.Button(buttons, text='Справка', command=self.help).pack(side='left', padx=8)
        self.clean_btn = ttk.Button(buttons, text='ОЧИСТИТЬ  ›', style='Gold.TButton', command=self.clean, state='disabled')
        self.clean_btn.pack(side='right')
        tk.Label(self, textvariable=self.status, bg=BG, fg=MUTED, font=('Segoe UI', 9), anchor='w').pack(fill='x', padx=24, pady=(0, 9))

    def select_game(self, game):
        self.paths[self.game.title] = self.path.get()
        self.game = game
        self.path.set(self.paths.get(game.title, ''))
        self.game_label.configure(text=game.title + '  /  Подготовка к переустановке')
        for title, widgets in self.cards.items():
            selected = title == game.title
            widgets[0].configure(highlightbackground=GOLD if selected else LINE)
            for w in widgets:
                w.configure(bg='#35413e' if selected else PANEL)
        self.invalidate()

    def browse(self):
        path = filedialog.askdirectory(title='Выбери корневую папку ' + self.game.title)
        if path:
            self.path.set(path)

    def auto(self):
        self.path.set('')
        self.scan()

    def invalidate(self):
        self.plan = []
        if hasattr(self, 'tree'):
            self.tree.delete(*self.tree.get_children())
            self.clean_btn.configure(state='disabled')
            self.report.configure(text='Настройки изменены. Выполни сканирование.')
            self.status.set('Ожидание сканирования')

    def scan(self):
        selected = {k for k, value in self.checked.items() if value.get()}
        self.plan, warnings = discover(self.game, selected, self.path.get().strip() or None)
        self.tree.delete(*self.tree.get_children())
        for item in self.plan:
            self.tree.insert('', 'end', values=(CATEGORIES[item.category][0], str(item.path)))
        self.report.configure(text=f'Найдено объектов: {len(self.plan)}. Двойной щелчок — полный путь.' +
                              ('\n' + '\n'.join(warnings[:2]) if warnings else ''))
        self.clean_btn.configure(state='normal' if self.plan and os.name == 'nt' else 'disabled')
        self.status.set('Проверка завершена. Файлы пока не изменены.' if self.plan else 'Ничего не найдено. Проверь путь и категории.')

    def show_path(self, _event):
        items = self.tree.selection()
        if items:
            messagebox.showinfo('Найденный путь', self.tree.item(items[0], 'values')[1], parent=self)

    def clean(self):
        if not self.plan or os.name != 'nt':
            return
        processes = running(self.game)
        if processes:
            messagebox.showwarning('Закрой программы', 'Сначала закрой:\n' + '\n'.join(processes), parent=self)
            return
        if not messagebox.askyesno('Подтверждение очистки', f'{self.game.title}\n\nПереместить {len(self.plan)} объектов в архив?\n'
                                    'Игровые файлы (если выбраны) будут недоступны до переустановки или восстановления.\n'
                                    'Свободное место не увеличится. Закрой игру и лаунчер.', parent=self):
            return
        try:
            log, moved, failed = quarantine(self.game, self.plan)
        except OSError as e:
            messagebox.showerror('Ошибка', str(e), parent=self)
            return
        self.scan()
        self.status.set(f'Перемещено: {len(moved)}. Ошибок: {len(failed)}.')
        message = f'Перемещено: {len(moved)}\nОшибок: {len(failed)}\n\n' + ('Теперь можно переустановить игру без модов.\n' if not failed else 'Не все файлы очищены. Проверь ошибки перед переустановкой.\n')
        message += '\nЖурнал: ' + str(log)
        if failed:
            message += '\n\n' + '\n'.join(failed[:8])
        messagebox.showinfo('Результат очистки', message, parent=self)

    def history(self):
        win = tk.Toplevel(self)
        win.title('Архивы и восстановление'); win.geometry('750x440'); win.configure(bg=BG)
        self.label(win, 'III.  АРХИВ ОПЕРАЦИЙ', 16, GOLD, True, serif=True).pack(anchor='w', padx=16, pady=14)
        logs = sorted(journal_dir().glob('*.json'), reverse=True)
        listing = tk.Listbox(win, bg=INSET, fg=INK, selectbackground='#465549', font=('Segoe UI', 11), borderwidth=0)
        listing.pack(fill='both', expand=True, padx=16, pady=(0, 12))
        valid = []
        for path in logs:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                count = sum(Path(x['backup']).exists() for x in data['entries'])
                listing.insert('end', f"{data['game']}  |  {path.stem[:15]}  |  объектов в архиве: {count}")
                valid.append(path)
            except (OSError, ValueError, KeyError):
                continue
        if not valid:
            listing.insert('end', 'Нет операций очистки.')
        def selected():
            index = listing.curselection()
            return valid[index[0]] if index and index[0] < len(valid) else None
        def restore_selected():
            path = selected()
            if not path or not messagebox.askyesno('Восстановление', 'Вернуть архивные файлы на исходные места? Существующие файлы не будут перезаписаны.', parent=win):
                return
            try:
                done, errors = restore(path)
                messagebox.showinfo('Результат', f'Восстановлено: {len(done)}\nОшибок: {len(errors)}\n' + '\n'.join(errors[:8]), parent=win)
            except (OSError, ValueError, KeyError) as e:
                messagebox.showerror('Ошибка', str(e), parent=win)
            win.destroy(); self.invalidate()
        def open_log():
            path = selected()
            if path and os.name == 'nt':
                os.startfile(str(path))
        bar = tk.Frame(win, bg=BG); bar.pack(fill='x', padx=16, pady=(0, 16))
        ttk.Button(bar, text='Открыть журнал', command=open_log).pack(side='left')
        ttk.Button(bar, text='Восстановить выбранную операцию', style='Gold.TButton', command=restore_selected).pack(side='right')

    def help(self):
        messagebox.showinfo('Paradox Cleaner', '1. Выбери игру и папку установки.\n2. Выбери категории и просканируй.\n'
                            '3. Закрой игру, лаунчер и Steam, затем подтверди очистку.\n4. Переустанови игру, отключи моды и -debug_mode.\n\n'
                            'Steam: сначала отпишись от модов и при необходимости удали игру обычным способом. '
                            'Подписки и Steam Cloud могут вернуть файлы.\n\n'
                            'Программа перемещает файлы в .ParadoxCleaner_Backup_* рядом с исходными папками. '
                            'Восстановление не перезаписывает новые файлы. Чтобы освободить место, удали проверенный архив вручную.\n\n'
                            'Общий Paradox Launcher и чужие игры не затрагиваются. Сама контрольная сумма не редактируется. '
                            'Она зависит от версии, исходных файлов и модов. Нестандартные пользовательские пути нужно проверять вручную.', parent=self)


if __name__ == '__main__':
    Cleaner().mainloop()
